"""Mash agent server runtime with HTTP request streaming."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig
from mash.tools.mcp import MCPToolAdapter

from ..core.agent import Agent
from ..core.config import SystemPrompt
from ..core.context import Context, MessageRole
from ..logging import AgentTraceEvent, CommandEvent, EventLogger, LLMEvent, LogEvent
from ..memory.compaction import compact_conversation
from ..memory.signals import build_default_signal_collector
from ..tools.runtime import RuntimeToolBuilder
from .errors import classify_error
from .http import MashAgentHTTPHandler, MashAgentHTTPServer
from .spec import AgentSpec
from .types import RuntimeTurnResult


@dataclass
class _RequestState:
    request_id: str
    agent_id: str
    session_id: str
    message: str
    turn_metadata: Dict[str, Any]
    created_at: float
    condition: threading.Condition
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False


class _EventMultiplexer(EventLogger):
    """Fan-out logger that writes to disk and notifies a callback."""

    def __init__(self, destination: Path, callback: Callable[[LogEvent], None]) -> None:
        super().__init__(destination)
        self._callback = callback

    def emit(self, event: LogEvent) -> None:
        super().emit(event)
        self._callback(event)


class MashAgentServer:
    """Runtime server that owns app execution state and HTTP request lifecycle."""

    def __init__(
        self,
        definition: AgentSpec,
        *,
        request_ttl_seconds: int = 3600,
        max_buffered_requests: int = 1000,
    ) -> None:
        self.definition = definition
        self.app_id = definition.get_agent_id()
        self.default_session_id = str(uuid.uuid4())
        self.request_ttl_seconds = max(1, int(request_ttl_seconds))
        self.max_buffered_requests = max(10, int(max_buffered_requests))

        self.has_mcp_manager = False
        self.mcp_manager: Optional[MCPManager] = None
        self._subagent_ids: list[str] = []

        self.store = definition.build_store()
        self.tools = definition.build_tools()
        self.skills = definition.build_skills()

        llm = definition.build_llm()
        config = definition.build_agent_config()
        if config.app_id != self.app_id:
            raise ValueError(
                "AgentSpec.get_agent_id() must match build_agent_config().app_id "
                f"(got {self.app_id!r} vs {config.app_id!r})"
            )

        self.agent = Agent(llm=llm, tools=self.tools, skills=self.skills, config=config)
        self.agent.set_signal_collector(build_default_signal_collector())
        self.system_prompt: SystemPrompt = self.agent.config.system_prompt

        self._active_request_id: Optional[str] = None
        self._active_request_lock = threading.Lock()
        self._tool_context = threading.local()

        self._request_queue: queue.Queue[Optional[_RequestState]] = queue.Queue()
        self._request_lock = threading.Lock()
        self._requests: dict[str, _RequestState] = {}
        self._request_order: list[str] = []
        self._shutdown_event = threading.Event()
        self._worker = threading.Thread(
            target=self._request_worker_loop,
            name=f"MashAgentWorker-{self.app_id}",
            daemon=True,
        )

        self._http_server: Optional[MashAgentHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

        self.event_logger = _EventMultiplexer(
            definition.get_log_destination(),
            self._handle_runtime_event,
        )
        self.agent.set_event_logger(self.event_logger, self.default_session_id)
        self.agent.llm.set_event_logger(
            self.event_logger,
            self.default_session_id,
            self.agent.config.app_id,
        )

        if definition.enable_runtime_tools():
            self.configure_runtime_tools()

        mcp_servers = definition.build_mcp_servers()
        if mcp_servers:
            self.mcp_manager = MCPManager(
                default_model=self.agent.llm.model,
                event_logger=self.event_logger,
                session_id=self.default_session_id,
                app_id=self.agent.config.app_id,
            )
            self.has_mcp_manager = True
            self.configure_remote_tools(mcp_servers)

        self._worker.start()
        definition.on_startup(self)

    @classmethod
    def from_spec(cls, definition: AgentSpec) -> "MashAgentServer":
        return cls(definition)

    def configure_runtime_tools(self) -> None:
        builder = RuntimeToolBuilder(
            store=self.store,
            app_id=self.agent.config.app_id,
            session_id_provider=self.get_current_processing_session_id,
            event_logger=self.event_logger,
        )
        for tool in builder.build_tools():
            self.agent.tools.register(tool)

    def configure_remote_tools(self, mcp_servers: Sequence[MCPServerConfig]) -> None:
        manager = self.mcp_manager
        if manager is None:
            return

        try:
            for server in mcp_servers:
                manager.add_server(
                    name=server.name,
                    url=server.url,
                    description=server.description,
                    headers=server.headers,
                    allowed_tools=server.allowed_tools,
                    auto_connect=True,
                )
                mcp_tools = manager.get_flattened_tools(prefix="mcp_")
                for mcp_tool in mcp_tools:
                    server_name = mcp_tool.get("metadata", {}).get("server")
                    original_name = mcp_tool.get("metadata", {}).get("original_name")
                    if not server_name or not original_name:
                        continue

                    def make_executor(srv_name: str, tool_name: str):
                        def executor(args):
                            try:
                                result = manager.call_tool(srv_name, tool_name, args)
                                return extract_mcp_text(result)
                            except Exception as exc:  # pragma: no cover
                                return f"Error: {exc}"

                        return executor

                    adapter = MCPToolAdapter.from_mcp_tool(
                        mcp_tool=mcp_tool,
                        executor=make_executor(server_name, original_name),
                        prefix="",
                    )
                    self.agent.tools.register(adapter)
        except MCPClientError:
            return

    def get_default_session_id(self) -> str:
        return self.default_session_id

    def get_event_logger(self) -> Any:
        """Return the runtime event logger."""
        return self.event_logger

    def set_chain_renderer(self, renderer: Any) -> None:
        """Attach chain-of-thought renderer to the runtime agent."""
        self.agent.set_chain_renderer(renderer)

    def get_model(self) -> str:
        """Return the configured model name."""
        return self.agent.llm.model

    def get_max_steps(self) -> int:
        """Return the configured max think/act steps."""
        return self.agent.config.max_steps

    def get_latest_preferences(self) -> Optional[Dict[str, Any]]:
        """Return latest persisted preferences for this app scope."""
        return self.store.get_latest_preferences(app_id=self.agent.config.app_id)

    def get_preferences(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return persisted preferences for one app/session scope."""
        return self.store.get_preferences(
            app_id=self.agent.config.app_id,
            session_id=session_id,
        )

    def set_preferences(self, session_id: str, preferences: Dict[str, Any]) -> None:
        """Persist preferences in app/session scope."""
        self.store.set_preferences(
            app_id=self.agent.config.app_id,
            session_id=session_id,
            preferences=preferences,
        )

    def list_app_data(self, session_id: str) -> list[dict[str, Any]]:
        """List app data entries for the active app/session scope."""
        return self.store.list_app_data(
            app_id=self.agent.config.app_id,
            session_id=session_id,
        )

    def get_app_data(self, session_id: str, key: str) -> Any:
        """Fetch one app data value by key."""
        return self.store.get_app_data(
            app_id=self.agent.config.app_id,
            session_id=session_id,
            key=key,
        )

    def set_app_data(self, session_id: str, key: str, value: Any) -> None:
        """Set one app data value by key."""
        self.store.set_app_data(
            app_id=self.agent.config.app_id,
            session_id=session_id,
            key=key,
            value=value,
        )

    def delete_app_data(self, session_id: str, key: str) -> bool:
        """Delete one app data entry by key."""
        return self.store.delete_app_data(
            app_id=self.agent.config.app_id,
            session_id=session_id,
            key=key,
        )

    def get_history_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return conversation history turns for the session."""
        return self.store.get_turns(session_id=session_id, limit=limit)

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return persisted sessions for this agent."""
        if not hasattr(self.store, "list_sessions"):
            return []
        return self.store.list_sessions(app_id=self.agent.config.app_id)

    def get_subagent_ids(self) -> list[str]:
        return list(self._subagent_ids)

    def set_subagent_ids(self, subagent_ids: Sequence[str]) -> None:
        """Set host-configured subagent IDs for runtime introspection."""
        seen: set[str] = set()
        ordered: list[str] = []
        for value in subagent_ids:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        self._subagent_ids = ordered

    def set_system_prompt(self, prompt: SystemPrompt) -> None:
        self.system_prompt = prompt
        self.agent.config.system_prompt = prompt

    def handle_control_request(
        self, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle runtime control actions used by HTTP clients."""
        action_name = action.strip()
        data = payload or {}
        if not action_name:
            raise ValueError("action is required")

        if action_name == "get_default_session_id":
            return {"default_session_id": self.get_default_session_id()}

        if action_name == "get_session_info":
            session_id_value = data.get("session_id")
            if session_id_value is None:
                session_id = self.get_default_session_id()
            elif isinstance(session_id_value, str):
                session_id = session_id_value.strip() or self.get_default_session_id()
            else:
                raise ValueError("session_id must be a string")
            return {
                "app_id": self.app_id,
                "agent_id": self.app_id,
                "session_id": session_id,
                "primary_agent_id": self.app_id,
                "subagent_ids": self.get_subagent_ids(),
                "model": self.get_model(),
                "max_steps": self.get_max_steps(),
                "session_total_tokens": self.get_session_total_tokens(session_id),
            }

        if action_name == "list_sessions":
            return {"sessions": self.list_sessions()}

        if action_name == "get_subagent_ids":
            return {"subagent_ids": self.get_subagent_ids()}

        if action_name == "set_subagent_ids":
            raw_ids = data.get("subagent_ids")
            if not isinstance(raw_ids, list):
                raise ValueError("subagent_ids must be an array")
            self.set_subagent_ids([str(value) for value in raw_ids])
            return {"ok": True}

        if action_name == "get_latest_preferences":
            return {"preferences": self.get_latest_preferences()}

        if action_name == "get_preferences":
            session_id = data.get("session_id")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            return {"preferences": self.get_preferences(session_id)}

        if action_name == "set_preferences":
            session_id = data.get("session_id")
            preferences = data.get("preferences")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            if not isinstance(preferences, dict):
                raise ValueError("preferences must be an object")
            self.set_preferences(session_id, preferences)
            return {"ok": True}

        if action_name == "list_app_data":
            session_id = data.get("session_id")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            return {"items": self.list_app_data(session_id)}

        if action_name == "get_app_data":
            session_id = data.get("session_id")
            key = data.get("key")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            if not isinstance(key, str):
                raise ValueError("key must be a string")
            return {"value": self.get_app_data(session_id, key)}

        if action_name == "set_app_data":
            session_id = data.get("session_id")
            key = data.get("key")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            if not isinstance(key, str):
                raise ValueError("key must be a string")
            self.set_app_data(session_id, key, data.get("value"))
            return {"ok": True}

        if action_name == "delete_app_data":
            session_id = data.get("session_id")
            key = data.get("key")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            if not isinstance(key, str):
                raise ValueError("key must be a string")
            return {"deleted": self.delete_app_data(session_id, key)}

        if action_name == "get_history_turns":
            session_id = data.get("session_id")
            if not isinstance(session_id, str):
                raise ValueError("session_id must be a string")
            raw_limit = data.get("limit")
            if raw_limit is None or raw_limit == "":
                limit = None
            else:
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as exc:
                    raise ValueError("limit must be an integer") from exc
            return {"turns": self.get_history_turns(session_id, limit=limit)}

        if action_name == "compact_session":
            raw_session_id = data.get("session_id")
            if raw_session_id is None:
                session_id = None
            elif isinstance(raw_session_id, str):
                session_id = raw_session_id
            else:
                raise ValueError("session_id must be a string")

            reason_value = data.get("reason", "manual")
            reason = str(reason_value)
            reset_value = data.get("session_total_tokens_reset", 0)
            try:
                session_total_tokens_reset = int(reset_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "session_total_tokens_reset must be an integer"
                ) from exc

            summary_text, turn_id = self.compact_session(
                session_id,
                reason=reason,
                session_total_tokens_reset=session_total_tokens_reset,
            )
            return {"summary_text": summary_text, "turn_id": turn_id}

        if action_name == "emit_command_event":
            raw_event = data.get("event")
            if not isinstance(raw_event, dict):
                raise ValueError("event must be an object")

            event_type_value = raw_event.get("event_type")
            if not isinstance(event_type_value, str) or not event_type_value.strip():
                raise ValueError("event.event_type is required")

            app_id_value = raw_event.get("app_id")
            if isinstance(app_id_value, str) and app_id_value.strip():
                app_id = app_id_value
            else:
                app_id = self.app_id

            raw_session_id = raw_event.get("session_id")
            if raw_session_id is None:
                session_id = None
            elif isinstance(raw_session_id, str):
                session_id = raw_session_id
            else:
                session_id = str(raw_session_id)

            raw_duration_ms = raw_event.get("duration_ms")
            if raw_duration_ms is None:
                duration_ms = None
            else:
                try:
                    duration_ms = int(raw_duration_ms)
                except (TypeError, ValueError):
                    duration_ms = None

            payload_value = raw_event.get("payload")
            payload_dict = payload_value if isinstance(payload_value, dict) else {}

            self.event_logger.emit(
                CommandEvent(
                    event_type=event_type_value,
                    app_id=app_id,
                    session_id=session_id,
                    payload=payload_dict,
                    command_name=(
                        raw_event["command_name"]
                        if isinstance(raw_event.get("command_name"), str)
                        else None
                    ),
                    args=(
                        raw_event["args"]
                        if isinstance(raw_event.get("args"), str)
                        else None
                    ),
                    duration_ms=duration_ms,
                    error=(
                        raw_event["error"]
                        if isinstance(raw_event.get("error"), str)
                        else None
                    ),
                    trace_id=(
                        raw_event["trace_id"]
                        if isinstance(raw_event.get("trace_id"), str)
                        else None
                    ),
                )
            )
            return {"ok": True}

        raise ValueError(f"unknown action: {action_name}")

    def get_current_processing_session_id(self) -> str:
        value = getattr(self._tool_context, "session_id", None)
        if isinstance(value, str) and value.strip():
            return value
        return self.default_session_id

    def get_session_total_tokens(self, session_id: str | None = None) -> int:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        turns = self.store.get_turns(session_id=target_session_id, limit=1)
        if not turns:
            return 0
        value = turns[-1].get("session_total_tokens", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def compact_session(
        self,
        session_id: str | None = None,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
    ) -> tuple[Optional[str], Optional[str]]:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        return compact_conversation(
            store=self.store,
            llm=self.agent.llm,
            app_id=self.agent.config.app_id,
            session_id=target_session_id,
            max_tokens=self.agent.config.max_tokens,
            temperature=self.agent.config.compaction_temperature,
            turn_limit=self.agent.config.compaction_turn_limit,
            reason=reason,
            session_total_tokens_reset=session_total_tokens_reset,
        )

    def build_context_with_history(self, session_id: str, message: str) -> Context:
        context = Context(system_prompt=self.system_prompt)

        if self.agent.config.conversation_history_turns > 0:
            turns = self.store.get_turns(session_id=session_id, limit=None)
            if turns:
                summary_index = None
                for idx in range(len(turns) - 1, -1, -1):
                    meta = turns[idx].get("metadata") or {}
                    if meta.get("type") == "summary_checkpoint":
                        summary_index = idx
                        break

                if summary_index is not None:
                    tail_turns = turns[summary_index + 1 :]
                    tail_turns = tail_turns[
                        -self.agent.config.conversation_history_turns :
                    ]
                    turns_to_include = [turns[summary_index]] + tail_turns
                else:
                    turns_to_include = turns[
                        -self.agent.config.conversation_history_turns :
                    ]

                for turn in turns_to_include:
                    meta = turn.get("metadata") or {}
                    user_text = turn.get("user_message")
                    if user_text and meta.get("type") != "summary_checkpoint":
                        context.add_message(
                            MessageRole.USER,
                            user_text,
                            source="history",
                            turn_id=turn.get("turn_id"),
                        )

                    agent_text = turn.get("agent_response")
                    if agent_text:
                        context.add_message(
                            MessageRole.ASSISTANT,
                            agent_text,
                            source="history",
                            turn_id=turn.get("turn_id"),
                        )

        context.add_user_message(message)
        return context

    def compute_turn_tokens(self, response_metadata: Dict[str, Any]) -> int:
        token_usage = response_metadata.get("token_usage")
        if not token_usage:
            return 0

        input_tokens = token_usage.get("input")
        output_tokens = token_usage.get("output")
        if input_tokens is None or output_tokens is None:
            return 0

        return int(input_tokens) + int(output_tokens)

    def process_user_message(
        self,
        message: str,
        session_id: str | None = None,
        turn_metadata: dict[str, Any] | None = None,
    ) -> RuntimeTurnResult:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        previous_session = getattr(self._tool_context, "session_id", None)
        previous_agent_session = self.agent.get_event_logger_session_id()
        llm_provider = self.agent.llm
        previous_llm_session = llm_provider.get_event_logger_session_id()
        self._tool_context.session_id = target_session_id
        self.agent.set_event_logger(self.event_logger, target_session_id)
        llm_provider.set_event_logger(
            self.event_logger,
            target_session_id,
            self.agent.config.app_id,
        )
        try:
            compaction_summary_text: Optional[str] = None
            compaction_summary_turn_id: Optional[str] = None

            if self.agent.config.compaction_token_threshold > 0:
                session_total_tokens = self.get_session_total_tokens(target_session_id)
                if session_total_tokens >= self.agent.config.compaction_token_threshold:
                    compaction_summary_text, compaction_summary_turn_id = (
                        self.compact_session(
                            target_session_id,
                            reason="auto",
                            session_total_tokens_reset=0,
                        )
                    )
                    if compaction_summary_text and self.event_logger:
                        self.event_logger.emit(
                            AgentTraceEvent(
                                event_type="agent.compaction",
                                app_id=self.agent.config.app_id,
                                session_id=target_session_id,
                                trace_id=None,
                                payload={
                                    "reason": "auto",
                                    "summary_turn_id": compaction_summary_turn_id,
                                    "compaction_token_threshold": self.agent.config.compaction_token_threshold,
                                    "session_total_tokens_before_compaction": session_total_tokens,
                                    "turn_limit": self.agent.config.compaction_turn_limit,
                                },
                            )
                        )

            context = self.build_context_with_history(target_session_id, message)
            response = self.agent.run(context)

            response_metadata = dict(response.metadata or {})
            token_usage = response_metadata.get("token_usage") or {}
            response_metadata["token_usage"] = token_usage
            if turn_metadata:
                response_metadata.update(turn_metadata)

            total_tokens = self.compute_turn_tokens(response_metadata)
            session_total_tokens = (
                self.get_session_total_tokens(target_session_id) + total_tokens
            )

            trace_id = response_metadata.get("trace_id")
            self.store.save_turn(
                trace_id=trace_id or str(uuid.uuid4()),
                session_id=target_session_id,
                app_id=self.agent.config.app_id,
                user_message=message,
                agent_response=response.text,
                signals=response.signals,
                session_total_tokens=session_total_tokens,
                metadata=response_metadata,
            )

            return RuntimeTurnResult(
                session_id=target_session_id,
                response=response,
                compaction_summary_text=compaction_summary_text,
                compaction_summary_turn_id=compaction_summary_turn_id,
                session_total_tokens=session_total_tokens,
            )
        finally:
            if previous_agent_session is not None:
                self.agent.set_event_logger(self.event_logger, previous_agent_session)
            if previous_llm_session is not None:
                llm_provider.set_event_logger(
                    self.event_logger,
                    previous_llm_session,
                    self.agent.config.app_id,
                )
            if previous_session is None:
                try:
                    del self._tool_context.session_id
                except AttributeError:
                    pass
            else:
                self._tool_context.session_id = previous_session

    def submit_request(
        self,
        *,
        message: str,
        session_id: str | None = None,
        turn_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        request_id = str(uuid.uuid4())
        state = _RequestState(
            request_id=request_id,
            agent_id=self.app_id,
            session_id=target_session_id,
            message=message,
            turn_metadata=dict(turn_metadata or {}),
            created_at=time.time(),
            condition=threading.Condition(),
        )

        accepted_payload = {
            "request_id": request_id,
            "agent_id": self.app_id,
            "status": "accepted",
            "session_id": target_session_id,
        }
        state.events.append({"event": "request.accepted", "data": accepted_payload})

        with self._request_lock:
            self._requests[request_id] = state
            self._request_order.append(request_id)

        self._request_queue.put(state)
        self._cleanup_request_buffers()
        return accepted_payload

    def has_request(self, request_id: str) -> bool:
        with self._request_lock:
            return request_id in self._requests

    def stream_request_events(
        self,
        request_id: str,
        *,
        cursor: int = 0,
        wait_timeout: float = 15.0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        with self._request_lock:
            state = self._requests.get(request_id)
        if state is None:
            raise KeyError(request_id)

        with state.condition:
            if cursor >= len(state.events) and not state.done:
                state.condition.wait(timeout=max(0.0, wait_timeout))
            events = state.events[cursor:]
            next_cursor = cursor + len(events)
            done = state.done

        return events, next_cursor, done

    def start_http_server(
        self,
        *,
        agent_id: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> str:
        if self._http_server is not None:
            sock = self._http_server.socket.getsockname()
            return f"http://{sock[0]}:{sock[1]}"

        resolved_agent_id = (agent_id or self.app_id).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")

        server = MashAgentHTTPServer(
            (host, port),
            MashAgentHTTPHandler,
            runtime=self,
            agent_id=resolved_agent_id,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
            name=f"MashAgentHTTP-{resolved_agent_id}",
        )
        thread.start()

        self._http_server = server
        self._http_thread = thread
        sock = server.socket.getsockname()
        return f"http://{sock[0]}:{sock[1]}"

    def stop_http_server(self) -> None:
        if self._http_server is None:
            return
        self._http_server.shutdown()
        self._http_server.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=2)
        self._http_server = None
        self._http_thread = None

    def _request_worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                state = self._request_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if state is None:
                break

            self._append_request_event(
                state.request_id,
                event="request.started",
                data={
                    "request_id": state.request_id,
                    "agent_id": self.app_id,
                    "session_id": state.session_id,
                    "status": "started",
                },
            )

            with self._active_request_lock:
                self._active_request_id = state.request_id

            try:
                result = self.process_user_message(
                    state.message,
                    session_id=state.session_id,
                    turn_metadata=state.turn_metadata,
                )
                self._append_request_event(
                    state.request_id,
                    event="request.completed",
                    data={
                        "request_id": state.request_id,
                        "agent_id": self.app_id,
                        "status": "completed",
                        "session_id": result.session_id,
                        "response": {
                            "text": result.response.text,
                            "signals": result.response.signals,
                            "metadata": result.response.metadata,
                        },
                        "compaction_summary_text": result.compaction_summary_text,
                        "compaction_summary_turn_id": result.compaction_summary_turn_id,
                        "session_total_tokens": result.session_total_tokens,
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive
                error_payload = classify_error(exc)
                self._append_request_event(
                    state.request_id,
                    event="request.error",
                    data={
                        "request_id": state.request_id,
                        "agent_id": self.app_id,
                        "status": "error",
                        "session_id": state.session_id,
                        **error_payload,
                    },
                )
            finally:
                self._mark_request_done(state.request_id)
                with self._active_request_lock:
                    self._active_request_id = None

    def _append_request_event(
        self, request_id: str, *, event: str, data: dict[str, Any]
    ) -> None:
        with self._request_lock:
            state = self._requests.get(request_id)
        if state is None:
            return

        with state.condition:
            state.events.append({"event": event, "data": data})
            state.condition.notify_all()

    def _mark_request_done(self, request_id: str) -> None:
        with self._request_lock:
            state = self._requests.get(request_id)
        if state is None:
            return

        with state.condition:
            state.done = True
            state.condition.notify_all()

    def _handle_runtime_event(self, event: Any) -> None:
        with self._active_request_lock:
            request_id = self._active_request_id
        if not request_id:
            return

        payload = self._to_trace_payload(event)
        if payload is None:
            return

        self._append_request_event(
            request_id,
            event="agent.trace",
            data=payload,
        )

    def _to_trace_payload(self, event: Any) -> Optional[dict[str, Any]]:
        if isinstance(event, AgentTraceEvent):
            return {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "step_id": event.step_id,
                "duration_ms": event.duration_ms,
                "action_type": event.action_type,
                "tool_calls": event.tool_calls,
                "token_usage": event.token_usage,
                "payload": dict(event.payload or {}),
            }

        if isinstance(event, LLMEvent):
            return {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "provider": event.provider,
                "model": event.model,
                "duration_ms": event.duration_ms,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "total_tokens": event.total_tokens,
                "finish_reason": event.finish_reason,
                "error": event.error,
                "tools": event.tools,
                "betas": event.betas,
            }

        return None

    def _cleanup_request_buffers(self) -> None:
        now = time.time()
        ttl_cutoff = now - float(self.request_ttl_seconds)

        with self._request_lock:
            kept_ids: list[str] = []
            for request_id in self._request_order:
                state = self._requests.get(request_id)
                if state is None:
                    continue
                expired = state.done and state.created_at < ttl_cutoff
                if expired:
                    self._requests.pop(request_id, None)
                    continue
                kept_ids.append(request_id)
            self._request_order = kept_ids

            if len(self._request_order) <= self.max_buffered_requests:
                return

            removable = len(self._request_order) - self.max_buffered_requests
            next_order: list[str] = []
            for request_id in self._request_order:
                state = self._requests.get(request_id)
                if state is None:
                    continue
                if removable > 0 and state.done:
                    self._requests.pop(request_id, None)
                    removable -= 1
                    continue
                next_order.append(request_id)
            self._request_order = next_order

    def shutdown(self) -> None:
        try:
            self.definition.on_shutdown(self)
        finally:
            self.stop_http_server()
            self._shutdown_event.set()
            self._request_queue.put(None)
            if self._worker.is_alive():
                self._worker.join(timeout=2)
            self._subagent_ids = []
            if self.has_mcp_manager and self.mcp_manager is not None:
                self.mcp_manager.disconnect_all()


def extract_mcp_text(result: Any) -> str:
    """Extract plain text output from an MCP tool result payload."""
    if isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return "\n".join(texts) if texts else str(result)
    return str(result)

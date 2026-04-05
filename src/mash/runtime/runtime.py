"""Core Mash agent runtime without transport concerns."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig
from mash.tools.mcp import MCPToolAdapter

from ..core.agent import Agent
from ..core.config import SystemPrompt
from ..core.context import Context, MessageRole
from ..logging import (
    AgentTraceEvent,
    CommandEvent,
    DebugEvent,
    EventLogger,
    LLMEvent,
    LogEvent,
)
from ..memory.compaction import compact_conversation
from ..memory.signals import build_default_signal_collector
from ..tools.runtime import RuntimeToolBuilder
from ..tools.subagent import InvokeSubagentTool
from .client import MashAgentClient
from .errors import classify_error
from .spec import AgentSpec
from .types import RuntimeTurnResult, SubagentEndpoint, SubAgentMetadata


@dataclass
class _RequestState:
    request_id: str
    agent_id: str
    session_id: str
    message: str
    turn_metadata: Dict[str, Any]
    created_at: float
    updated_event: asyncio.Event
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "accepted"
    done: bool = False
    started_at: float | None = None
    completed_at: float | None = None
    task: asyncio.Task[None] | None = None


class _EventMultiplexer(EventLogger):
    """Fan-out logger that writes to storage and notifies a callback."""

    def __init__(
        self,
        store: Any,
        callback: Callable[[LogEvent], Awaitable[None] | None],
    ) -> None:
        super().__init__(store)
        self._callback = callback

    async def emit(self, event: LogEvent) -> None:
        await super().emit(event)
        callback_result = self._callback(event)
        if inspect.isawaitable(callback_result):
            await callback_result


def build_subagent_prompt_block(
    base_prompt: SystemPrompt,
    subagents: Dict[str, SubAgentMetadata],
) -> SystemPrompt:
    """Append subagent routing guidance to a system prompt."""
    if not subagents:
        return base_prompt

    lines = [
        "SUBAGENTS",
        "Delegate work using InvokeSubagent(agent_id, prompt, opts).",
    ]
    for agent_id in sorted(subagents.keys()):
        meta = subagents[agent_id]
        capabilities = ", ".join(meta.capabilities)
        lines.append(f"- {agent_id} | {meta.display_name}: {meta.description}")
        lines.append(f"  Capabilities: {capabilities}")
        lines.append(f"  Guidance: {meta.usage_guidance}")
    lines.append(
        "When delegating, choose the best subagent id and pass a concise task prompt."
    )
    guidance = "\n".join(lines)

    if isinstance(base_prompt, list):
        return [*base_prompt, {"type": "text", "text": guidance}]
    return f"{base_prompt}\n\n{guidance}"


class MashAgentRuntime:
    """Async-native runtime core that owns request lifecycle and execution state."""

    def __init__(
        self,
        definition: AgentSpec,
        *,
        request_ttl_seconds: int = 3600,
        max_buffered_requests: int = 1000,
        max_concurrent_requests: int = 4,
    ) -> None:
        self.definition = definition
        self.app_id = definition.get_agent_id()
        self.default_session_id = str(uuid.uuid4())
        self.request_ttl_seconds = max(1, int(request_ttl_seconds))
        self.max_buffered_requests = max(10, int(max_buffered_requests))
        self.max_concurrent_requests = max(1, int(max_concurrent_requests))

        self.store = definition.build_store()
        self.event_logger = _EventMultiplexer(self.store, self._handle_runtime_event)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._requests: dict[str, _RequestState] = {}
        self._request_order: list[str] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_refcounts: dict[str, int] = {}
        self._shutdown_started = False
        self._shutdown_complete = False

        self._subagent_ids: list[str] = []
        self._subagent_clients: dict[str, Any] = {}
        self._subagent_metadata: dict[str, SubAgentMetadata] = {}
        self._mcp_servers = definition.build_mcp_servers()
        self.has_mcp_manager = False
        self.mcp_manager: Optional[MCPManager] = None
        self._chain_renderer: Any = None

        self._current_request_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar(
                f"mash_request_id_{self.app_id}",
                default=None,
            )
        )
        self._current_session_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar(
                f"mash_session_id_{self.app_id}",
                default=None,
            )
        )

        self.agent = self._build_agent_instance()
        self.agent.set_event_logger(self.event_logger, self.default_session_id)
        self.agent.llm.set_event_logger(
            self.event_logger,
            self.default_session_id,
            self.app_id,
        )
        self.system_prompt: SystemPrompt = self.agent.config.system_prompt
        self.tools = self.agent.tools
        self.skills = self.agent.skills

        definition.on_startup(self)

    @classmethod
    def from_spec(cls, definition: AgentSpec) -> "MashAgentRuntime":
        return cls(definition)

    def _build_agent_instance(self) -> Agent:
        tools = self.definition.build_tools()
        skills = self.definition.build_skills()
        llm = self.definition.build_llm()
        if hasattr(self, "agent"):
            config = replace(self.agent.config)
        else:
            config = self.definition.build_agent_config()
        if config.app_id != self.app_id:
            raise ValueError(
                "AgentSpec.get_agent_id() must match build_agent_config().app_id "
                f"(got {self.app_id!r} vs {config.app_id!r})"
            )
        configured_prompt = getattr(self, "system_prompt", None)
        if configured_prompt is not None:
            config.system_prompt = configured_prompt

        agent = Agent(llm=llm, tools=tools, skills=skills, config=config)
        agent.set_signal_collector(build_default_signal_collector())
        if self._chain_renderer is not None:
            agent.set_chain_renderer(self._chain_renderer)
        if self.definition.enable_runtime_tools():
            self.configure_runtime_tools(agent)
        if self._mcp_servers:
            self.configure_remote_tools(agent, self._mcp_servers)
        if self._subagent_clients:
            self._configure_subagent_tools(agent)
        return agent

    def configure_runtime_tools(self, agent: Agent) -> None:
        builder = RuntimeToolBuilder(
            store=self.store,
            app_id=self.app_id,
            session_id_provider=self.get_current_processing_session_id,
            event_logger=self.event_logger,
        )
        for tool in builder.build_tools():
            agent.tools.register(tool)

    def configure_remote_tools(
        self,
        agent: Agent,
        mcp_servers: Sequence[MCPServerConfig],
    ) -> None:
        if self.mcp_manager is None:
            self.mcp_manager = MCPManager(
                default_model=agent.llm.model,
                event_logger=self.event_logger,
                session_id=self.default_session_id,
                app_id=self.app_id,
            )
            self.has_mcp_manager = True

        manager = self.mcp_manager
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
                    if adapter.name not in agent.tools:
                        agent.tools.register(adapter)
        except MCPClientError:
            return

    def _configure_subagent_tools(self, agent: Agent) -> None:
        agent.config.system_prompt = self.system_prompt
        if "InvokeSubagent" in agent.tools:
            agent.tools.unregister("InvokeSubagent")
        agent.tools.register(
            InvokeSubagentTool(
                client_resolver=self.get_subagent_client,
                primary_app_id=self.app_id,
                primary_session_id_provider=self.get_current_processing_session_id,
                event_logger=self.get_event_logger(),
            )
        )

    def configure_subagents(self, subagents: Sequence[SubagentEndpoint]) -> None:
        resolved = {
            item.agent_id: item.metadata
            for item in subagents
            if item.agent_id and item.base_url and item.metadata is not None
        }
        self._subagent_metadata = resolved
        self.set_subagent_ids(sorted(resolved.keys()))

        self._subagent_clients = {
            item.agent_id: MashAgentClient(item.base_url, item.agent_id)
            for item in subagents
            if item.agent_id and item.base_url
        }
        if resolved:
            self.set_system_prompt(
                build_subagent_prompt_block(self.system_prompt, resolved)
            )
            self._configure_subagent_tools(self.agent)

    def get_subagent_client(self, agent_id: str) -> Any:
        client = self._subagent_clients.get(agent_id)
        if client is None:
            raise ValueError(f"subagent client '{agent_id}' is not configured")
        return client

    def get_default_session_id(self) -> str:
        return self.default_session_id

    def get_event_logger(self) -> Any:
        return self.event_logger

    def set_chain_renderer(self, renderer: Any) -> None:
        self._chain_renderer = renderer
        self.agent.set_chain_renderer(renderer)

    def get_model(self) -> str:
        return self.agent.llm.model

    def get_max_steps(self) -> int:
        return self.agent.config.max_steps

    async def open(self) -> None:
        await self.store.open()

    async def get_session_info(self, session_id: str | None = None) -> dict[str, Any]:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id
        return {
            "app_id": self.app_id,
            "agent_id": self.app_id,
            "session_id": target_session_id,
            "primary_agent_id": self.app_id,
            "subagent_ids": self.get_subagent_ids(),
            "model": self.get_model(),
            "max_steps": self.get_max_steps(),
            "max_concurrent_requests": self.max_concurrent_requests,
            "active_request_count": sum(
                1 for state in self._requests.values() if not state.done
            ),
            "session_total_tokens": await self.get_session_total_tokens(
                target_session_id
            ),
        }

    async def get_latest_preferences(self) -> Optional[Dict[str, Any]]:
        return await self.store.get_latest_preferences(app_id=self.app_id)

    async def get_preferences(self, session_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_preferences(
            app_id=self.app_id,
            session_id=session_id,
        )

    async def set_preferences(
        self, session_id: str, preferences: Dict[str, Any]
    ) -> None:
        await self.store.set_preferences(
            app_id=self.app_id,
            session_id=session_id,
            preferences=preferences,
        )

    async def list_app_data(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.list_app_data(
            app_id=self.app_id,
            session_id=session_id,
        )

    async def get_app_data(self, session_id: str, key: str) -> Any:
        return await self.store.get_app_data(
            app_id=self.app_id,
            session_id=session_id,
            key=key,
        )

    async def set_app_data(self, session_id: str, key: str, value: Any) -> None:
        await self.store.set_app_data(
            app_id=self.app_id,
            session_id=session_id,
            key=key,
            value=value,
        )

    async def delete_app_data(self, session_id: str, key: str) -> bool:
        return await self.store.delete_app_data(
            app_id=self.app_id,
            session_id=session_id,
            key=key,
        )

    async def get_history_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self.store.get_turns(session_id=session_id, limit=limit)

    async def list_sessions(self) -> list[dict[str, Any]]:
        if not hasattr(self.store, "list_sessions"):
            return []
        return await self.store.list_sessions(app_id=self.app_id)

    def get_subagent_ids(self) -> list[str]:
        return list(self._subagent_ids)

    def set_subagent_ids(self, subagent_ids: Sequence[str]) -> None:
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

    def get_current_processing_session_id(self) -> str:
        value = self._current_session_id.get()
        if isinstance(value, str) and value.strip():
            return value
        return self.default_session_id

    async def get_session_total_tokens(self, session_id: str | None = None) -> int:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        turns = await self.store.get_turns(session_id=target_session_id, limit=1)
        if not turns:
            return 0
        value = turns[-1].get("session_total_tokens", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def compact_session(
        self,
        session_id: str | None = None,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
    ) -> tuple[Optional[str], Optional[str]]:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        llm = self.definition.build_llm()
        if hasattr(llm, "set_event_logger"):
            llm.set_event_logger(self.event_logger, target_session_id, self.app_id)

        return await compact_conversation(
            store=self.store,
            llm=llm,
            app_id=self.app_id,
            session_id=target_session_id,
            max_tokens=self.agent.config.max_tokens,
            temperature=self.agent.config.compaction_temperature,
            turn_limit=self.agent.config.compaction_turn_limit,
            reason=reason,
            session_total_tokens_reset=session_total_tokens_reset,
        )

    async def build_context_with_history(
        self, session_id: str, message: str
    ) -> Context:
        context = Context(system_prompt=self.system_prompt)

        if self.agent.config.conversation_history_turns > 0:
            turns = await self.store.get_turns(session_id=session_id, limit=None)
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

    async def _run_agent(self, agent: Agent, context: Context):
        result = agent.run(context)
        if inspect.isawaitable(result):
            return await result
        return result

    async def process_user_message(
        self,
        message: str,
        session_id: str | None = None,
        turn_metadata: dict[str, Any] | None = None,
    ) -> RuntimeTurnResult:
        target_session_id = (session_id or self.default_session_id).strip()
        if not target_session_id:
            target_session_id = self.default_session_id

        agent = self._build_agent_instance()
        agent.set_event_logger(self.event_logger, target_session_id)
        agent.llm.set_event_logger(self.event_logger, target_session_id, self.app_id)
        session_token = self._current_session_id.set(target_session_id)
        try:
            compaction_summary_text: Optional[str] = None
            compaction_summary_turn_id: Optional[str] = None

            if agent.config.compaction_token_threshold > 0:
                session_total_tokens = await self.get_session_total_tokens(
                    target_session_id,
                )
                if session_total_tokens >= agent.config.compaction_token_threshold:
                    compaction_summary_text, compaction_summary_turn_id = (
                        await self.compact_session(
                            target_session_id,
                            reason="auto",
                            session_total_tokens_reset=0,
                        )
                    )
                    if compaction_summary_text:
                        await self.event_logger.emit(
                            DebugEvent(
                                event_type="agent.compaction",
                                app_id=self.app_id,
                                session_id=target_session_id,
                                payload={
                                    "reason": "auto",
                                    "summary_turn_id": compaction_summary_turn_id,
                                    "compaction_token_threshold": agent.config.compaction_token_threshold,
                                    "session_total_tokens_before_compaction": session_total_tokens,
                                    "turn_limit": agent.config.compaction_turn_limit,
                                },
                            )
                        )

            context = await self.build_context_with_history(
                target_session_id,
                message,
            )
            response = await self._run_agent(agent, context)

            response_metadata = dict(response.metadata or {})
            token_usage = response_metadata.get("token_usage") or {}
            response_metadata["token_usage"] = token_usage
            if turn_metadata:
                response_metadata.update(turn_metadata)

            total_tokens = self.compute_turn_tokens(response_metadata)
            session_total_tokens = await self.get_session_total_tokens(
                target_session_id
            )
            session_total_tokens += total_tokens

            trace_id = response_metadata.get("trace_id")
            await self.store.save_turn(
                trace_id=trace_id or str(uuid.uuid4()),
                session_id=target_session_id,
                app_id=self.app_id,
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
            self._current_session_id.reset(session_token)
            await agent.tools.shutdown()

    async def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self.max_concurrent_requests)
            await self.open()
        elif self._loop is not loop:
            raise RuntimeError("MashAgentRuntime is bound to a different event loop")
        return loop

    def _notify_state_change(self, state: _RequestState) -> None:
        current = state.updated_event
        state.updated_event = asyncio.Event()
        current.set()

    def _increment_session_ref(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        self._session_refcounts[session_id] = (
            self._session_refcounts.get(session_id, 0) + 1
        )
        return lock

    def _decrement_session_ref(self, session_id: str) -> None:
        remaining = self._session_refcounts.get(session_id, 0) - 1
        if remaining > 0:
            self._session_refcounts[session_id] = remaining
            return
        self._session_refcounts.pop(session_id, None)
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            self._session_locks.pop(session_id, None)

    async def submit_request(
        self,
        *,
        message: str,
        session_id: str | None = None,
        turn_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        await self._ensure_loop()
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
            updated_event=asyncio.Event(),
        )

        accepted_payload = {
            "request_id": request_id,
            "agent_id": self.app_id,
            "status": "accepted",
            "session_id": target_session_id,
        }
        state.events.append({"event": "request.accepted", "data": accepted_payload})
        self._requests[request_id] = state
        self._request_order.append(request_id)
        self._increment_session_ref(target_session_id)
        state.task = asyncio.create_task(
            self._execute_request(state),
            name=f"MashAgentRequest-{self.app_id}-{request_id}",
        )
        self._cleanup_request_buffers()
        return accepted_payload

    def has_request(self, request_id: str) -> bool:
        return request_id in self._requests

    async def stream_request_events(
        self,
        request_id: str,
        *,
        cursor: int = 0,
        wait_timeout: float = 15.0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        await self._ensure_loop()
        state = self._requests.get(request_id)
        if state is None:
            raise KeyError(request_id)

        while True:
            events = state.events[cursor:]
            next_cursor = cursor + len(events)
            if events or state.done or wait_timeout <= 0:
                return events, next_cursor, state.done

            pending_event = state.updated_event
            try:
                await asyncio.wait_for(
                    pending_event.wait(), timeout=max(0.0, wait_timeout)
                )
            except asyncio.TimeoutError:
                return [], cursor, state.done

    async def _execute_request(self, state: _RequestState) -> None:
        semaphore = self._semaphore
        if semaphore is None:
            raise RuntimeError("runtime loop is not initialized")

        request_token = self._current_request_id.set(state.request_id)
        try:
            async with semaphore:
                session_lock = self._session_locks[state.session_id]
                if session_lock.locked():
                    self._append_request_event_now(
                        state.request_id,
                        event="request.waiting",
                        data={
                            "request_id": state.request_id,
                            "agent_id": self.app_id,
                            "session_id": state.session_id,
                            "status": "waiting",
                            "reason": "session_busy",
                        },
                        status="waiting",
                    )

                async with session_lock:
                    self._append_request_event_now(
                        state.request_id,
                        event="request.started",
                        data={
                            "request_id": state.request_id,
                            "agent_id": self.app_id,
                            "session_id": state.session_id,
                            "status": "started",
                        },
                        status="started",
                    )
                    try:
                        result = await self.process_user_message(
                            state.message,
                            session_id=state.session_id,
                            turn_metadata=state.turn_metadata,
                        )
                        self._append_request_event_now(
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
                            status="completed",
                        )
                    except asyncio.CancelledError:
                        self._append_request_event_now(
                            state.request_id,
                            event="request.error",
                            data={
                                "request_id": state.request_id,
                                "agent_id": self.app_id,
                                "status": "error",
                                "session_id": state.session_id,
                                "error": "request cancelled",
                                "error_code": "request_cancelled",
                                "retryable": False,
                            },
                            status="error",
                        )
                        raise
                    except Exception as exc:  # pragma: no cover - defensive
                        self._append_request_event_now(
                            state.request_id,
                            event="request.error",
                            data={
                                "request_id": state.request_id,
                                "agent_id": self.app_id,
                                "status": "error",
                                "session_id": state.session_id,
                                **classify_error(exc),
                            },
                            status="error",
                        )
        finally:
            self._current_request_id.reset(request_token)
            self._mark_request_done_now(state.request_id)
            self._decrement_session_ref(state.session_id)

    def _append_request_event_now(
        self,
        request_id: str,
        *,
        event: str,
        data: dict[str, Any],
        status: str | None = None,
    ) -> None:
        state = self._requests.get(request_id)
        if state is None:
            return
        state.events.append({"event": event, "data": data})
        if status is not None:
            state.status = status
        if event == "request.started":
            state.started_at = time.time()
        if event in {"request.completed", "request.error"}:
            state.completed_at = time.time()
        self._notify_state_change(state)

    def _mark_request_done_now(self, request_id: str) -> None:
        state = self._requests.get(request_id)
        if state is None:
            return
        state.done = True
        if state.completed_at is None:
            state.completed_at = time.time()
        self._notify_state_change(state)
        self._cleanup_request_buffers()

    async def _handle_runtime_event(self, event: Any) -> None:
        request_id = self._current_request_id.get()
        if not request_id:
            return

        payload = self._to_trace_payload(event)
        if payload is None:
            return

        self._append_request_event_now(
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

        if isinstance(event, CommandEvent):
            return {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "duration_ms": event.duration_ms,
                "payload": dict(event.payload or {}),
            }

        if isinstance(event, DebugEvent):
            return {
                "event_type": event.event_type,
                "payload": dict(event.payload or {}),
            }

        return None

    def _cleanup_request_buffers(self) -> None:
        now = time.time()
        ttl_cutoff = now - float(self.request_ttl_seconds)

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

    async def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_started = True
        try:
            tasks = [
                state.task
                for state in self._requests.values()
                if state.task is not None and not state.task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            self._subagent_ids = []
            for client in self._subagent_clients.values():
                close = getattr(client, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            self._subagent_clients.clear()

            if self.has_mcp_manager and self.mcp_manager is not None:
                self.mcp_manager.disconnect_all()
            await self.agent.tools.shutdown()
            await self.store.close()
        finally:
            self.definition.on_shutdown(self)
            self._shutdown_complete = True


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

"""Core Mash agent runtime without transport concerns."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig
from mash.tools.mcp import MCPToolAdapter

from ..core.agent import Agent
from ..core.config import SystemPrompt
from ..core.context import (
    Action,
    ActionType,
    Context,
    Message,
    MessageRole,
    Response,
    ToolCall,
)
from ..core.context import (
    ToolResult as ContextToolResult,
)
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
from .execution import (
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRecoveryManager,
    RuntimeReplayState,
    RuntimeWorkflowExecutor,
)
from .spec import AgentSpec
from .types import SubagentEndpoint, SubAgentMetadata


@dataclass
class _RequestState:
    request_id: str
    agent_id: str
    updated_event: asyncio.Event
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

        self.memory_store = definition.build_memory_store()
        self.runtime_store = definition.build_runtime_store()
        self.store = self.memory_store
        self.event_logger = _EventMultiplexer(
            self.memory_store, self._handle_runtime_event
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._requests: dict[str, _RequestState] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_refcounts: dict[str, int] = {}
        self._shutdown_started = False
        self._shutdown_complete = False
        self._recovery_started = False

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
        self._current_trace_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar(
                f"mash_trace_id_{self.app_id}",
                default=None,
            )
        )
        self._request_waiters: dict[str, asyncio.Event] = {}
        self._workflow_executor = RuntimeWorkflowExecutor(self)
        self._recovery_manager = RuntimeRecoveryManager(self)

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
            store=self.memory_store,
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
                if manager.get_server(server.name) is None:
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
        await self.runtime_store.open()
        if not self._recovery_started:
            self._recovery_started = True
            await self._recovery_manager.recover()

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

    async def build_context_payload(
        self,
        *,
        session_id: str,
        message: str,
    ) -> dict[str, Any]:
        compaction_summary_text: Optional[str] = None
        compaction_summary_turn_id: Optional[str] = None
        if self.agent.config.compaction_token_threshold > 0:
            session_total_tokens = await self.get_session_total_tokens(session_id)
            if session_total_tokens >= self.agent.config.compaction_token_threshold:
                compaction_summary_text, compaction_summary_turn_id = (
                    await self.compact_session(
                        session_id,
                        reason="auto",
                        session_total_tokens_reset=0,
                    )
                )
        context = await self.build_context_with_history(session_id, message)
        return {
            "context": self._serialize_context(context),
            "compaction": {
                "compaction_summary_text": compaction_summary_text,
                "compaction_summary_turn_id": compaction_summary_turn_id,
            },
        }

    def rebuild_context(self, state: RuntimeReplayState) -> Context:
        if state.context_payload is None:
            raise RuntimeError("context has not been loaded")
        context = self._deserialize_context(state.context_payload.get("context") or {})
        for loop_index in sorted(state.loop_actions.keys()):
            action = self.action_from_payload(state.loop_actions[loop_index])
            assistant_blocks = (
                state.loop_actions[loop_index].get("assistant_blocks") or []
            )
            if assistant_blocks:
                context.add_message(
                    MessageRole.ASSISTANT,
                    assistant_blocks,
                    stop_reason=state.loop_actions[loop_index].get("stop_reason"),
                )
            results = self._replay_tool_results(state, loop_index)
            if results:
                context = self.agent.observe(context, action, results)
        return context

    def action_from_payload(self, payload: dict[str, Any]) -> Action:
        action_type = str(payload.get("action_type") or "")
        if action_type == ActionType.TOOL_CALL.value:
            tool_calls = [
                ToolCall(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=dict(item.get("arguments") or {}),
                )
                for item in payload.get("tool_calls", [])
                if isinstance(item, dict)
            ]
            return Action.from_tool_calls(tool_calls, metadata=dict(payload or {}))
        if action_type == ActionType.RESPONSE.value:
            return Action.from_response(
                str(payload.get("assistant_text") or ""),
                metadata=dict(payload or {}),
            )
        return Action.finish(metadata=dict(payload or {}))

    async def run_durable_think(
        self,
        *,
        context: Context,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        agent = self._build_agent_instance()
        agent.set_event_logger(self.event_logger, session_id)
        agent.llm.set_event_logger(self.event_logger, session_id, self.app_id)
        agent.set_trace_id(trace_id)
        session_token = self._current_session_id.set(session_id)
        trace_token = self._current_trace_id.set(trace_id)
        started_at = time.time()
        try:
            action = await agent.think(context)
            return {
                "action_type": action.type.value,
                "assistant_text": action.metadata.get("assistant_text"),
                "assistant_blocks": list(action.metadata.get("assistant_blocks") or []),
                "stop_reason": action.metadata.get("stop_reason"),
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": dict(tool_call.arguments or {}),
                    }
                    for tool_call in action.tool_calls
                ],
                "token_usage": dict(action.metadata.get("token_usage") or {}),
                "tool_usage": agent.get_trace_tool_usage(),
                "duration_ms": int((time.time() - started_at) * 1000),
                "trace_id": action.metadata.get("trace_id") or trace_id,
            }
        finally:
            self._current_trace_id.reset(trace_token)
            self._current_session_id.reset(session_token)
            await agent.tools.shutdown()

    async def run_durable_tool_call(
        self,
        *,
        tool_call: ToolCall,
        session_id: str,
        trace_id: str,
    ) -> ContextToolResult:
        agent = self._build_agent_instance()
        agent.set_event_logger(self.event_logger, session_id)
        agent.llm.set_event_logger(self.event_logger, session_id, self.app_id)
        agent.set_trace_id(trace_id)
        session_token = self._current_session_id.set(session_id)
        trace_token = self._current_trace_id.set(trace_id)
        try:
            return await agent.execute_tool_call(tool_call)
        finally:
            self._current_trace_id.reset(trace_token)
            self._current_session_id.reset(session_token)
            await agent.tools.shutdown()

    def collect_durable_signals(
        self,
        context: Context,
        action: Action,
        results: list[ContextToolResult],
    ) -> dict[str, Any]:
        original_tool_usage = self.agent.get_trace_tool_usage()
        tool_usage = {
            str(name): {
                "tokens": int(entry.get("tokens", 0) or 0),
                "invocations": int(entry.get("invocations", 0) or 0),
            }
            for name, entry in dict(action.metadata.get("tool_usage") or {}).items()
            if isinstance(entry, dict)
        }
        for result in results:
            tool_name = str(result.metadata.get("tool_name") or "").strip()
            if not tool_name:
                continue
            entry = tool_usage.get(tool_name)
            if entry is None:
                tool_usage[tool_name] = {"tokens": 0, "invocations": 1}
            else:
                entry["invocations"] += 1
        self.agent.set_trace_tool_usage(tool_usage)
        try:
            return self.agent.collect_signals(context, action, results)
        finally:
            self.agent.set_trace_tool_usage(original_tool_usage)

    async def persist_durable_turn(
        self,
        *,
        message: str,
        session_id: str,
        response: Response,
        signals: dict[str, Any],
        compaction_payload: dict[str, Any],
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response_metadata = dict(response.metadata or {})
        token_usage = response_metadata.get("token_usage") or {}
        response_metadata["token_usage"] = token_usage
        if extra_metadata:
            response_metadata.update(dict(extra_metadata))
        total_tokens = self.compute_turn_tokens(response_metadata)
        session_total_tokens = await self.get_session_total_tokens(session_id)
        session_total_tokens += total_tokens
        trace_id = response_metadata.get("trace_id")
        resolved_trace_id = str(trace_id or uuid.uuid4())
        await self.store.save_turn(
            trace_id=resolved_trace_id,
            session_id=session_id,
            app_id=self.app_id,
            user_message=message,
            agent_response=response.text,
            signals=signals,
            session_total_tokens=session_total_tokens,
            metadata=response_metadata,
        )
        return {
            "turn_id": resolved_trace_id,
            "trace_id": resolved_trace_id,
            "session_total_tokens": session_total_tokens,
            "signals": dict(signals or {}),
            "response_metadata": dict(response_metadata or {}),
            "compaction_summary_text": compaction_payload.get(
                "compaction_summary_text"
            ),
            "compaction_summary_turn_id": compaction_payload.get(
                "compaction_summary_turn_id"
            ),
        }

    def get_terminal_action(self, state: RuntimeReplayState) -> Action | None:
        if not state.loop_actions:
            return None
        loop_index = max(state.loop_actions.keys())
        return self.action_from_payload(state.loop_actions[loop_index])

    def get_replayed_token_usage(self, state: RuntimeReplayState) -> dict[str, int]:
        input_tokens = 0
        output_tokens = 0
        for payload in state.loop_actions.values():
            usage = payload.get("token_usage") or {}
            input_tokens += int(usage.get("input") or 0)
            output_tokens += int(usage.get("output") or 0)
        return {"input": input_tokens, "output": output_tokens}

    def _replay_tool_results(
        self,
        state: RuntimeReplayState,
        loop_index: int,
    ) -> list[ContextToolResult]:
        results: list[ContextToolResult] = []
        for event in state.loop_results.get(loop_index, []):
            result_payload = dict(event.payload.get("result") or {})
            result_metadata = dict(result_payload.get("metadata") or {})
            tool_name = event.payload.get("tool_name")
            if tool_name is not None:
                result_metadata["tool_name"] = str(tool_name)
            results.append(
                ContextToolResult(
                    tool_call_id=str(event.payload.get("tool_call_id") or ""),
                    content=str(result_payload.get("content") or ""),
                    is_error=bool(result_payload.get("is_error")),
                    metadata=result_metadata,
                )
            )
        return results

    def replay_all_tool_results(
        self,
        state: RuntimeReplayState,
    ) -> list[ContextToolResult]:
        results: list[ContextToolResult] = []
        for loop_index in sorted(state.loop_results.keys()):
            results.extend(self._replay_tool_results(state, loop_index))
        return results

    def _serialize_context(self, context: Context) -> dict[str, Any]:
        return {
            "system_prompt": context.system_prompt,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    "metadata": dict(message.metadata or {}),
                }
                for message in context.messages
            ],
            "metadata": dict(context.metadata or {}),
            "signals": dict(context.signals or {}),
            "is_complete": bool(context.is_complete),
        }

    def _deserialize_context(self, payload: dict[str, Any]) -> Context:
        context = Context(
            system_prompt=payload.get("system_prompt") or self.system_prompt,
            metadata=dict(payload.get("metadata") or {}),
            signals=dict(payload.get("signals") or {}),
            is_complete=bool(payload.get("is_complete")),
        )
        for item in payload.get("messages", []):
            if not isinstance(item, dict):
                continue
            role_text = str(item.get("role") or MessageRole.USER.value)
            try:
                role = MessageRole(role_text)
            except ValueError:
                role = MessageRole.USER
            context.messages.append(
                Message(
                    role=role,
                    content=item.get("content", ""),
                    metadata=dict(item.get("metadata") or {}),
                )
            )
        return context

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
            updated_event=asyncio.Event(),
        )
        self._requests[request_id] = state
        self._increment_session_ref(target_session_id)
        accepted_event = await self.append_runtime_event(
            RuntimeEvent(
                request_id=request_id,
                app_id=self.app_id,
                agent_id=self.app_id,
                session_id=target_session_id,
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                payload={
                    "message": message,
                    "initial_session_id": target_session_id,
                    "turn_metadata": dict(turn_metadata or {}),
                },
            )
        )
        self.schedule_request_execution(request_id)
        return self._to_public_event(accepted_event)["data"]

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
        if not await self.runtime_store.has_request(request_id):
            raise KeyError(request_id)

        while True:
            stored_events = await self.runtime_store.list_events(
                request_id,
                after_seq=max(0, int(cursor)),
            )
            public_events = [self._to_public_event(event) for event in stored_events]
            next_cursor = int(cursor)
            if stored_events:
                next_cursor = int(stored_events[-1].seq)
            replay_state = await self._workflow_executor.load_state(request_id)
            done = replay_state.is_terminal
            if done and not any(
                event["event"] in {"request.completed", "request.error"}
                for event in public_events
            ):
                stored_events = await self.runtime_store.list_events(
                    request_id,
                    after_seq=max(0, int(cursor)),
                )
                public_events = [
                    self._to_public_event(event) for event in stored_events
                ]
                if stored_events:
                    next_cursor = int(stored_events[-1].seq)
            if public_events or done or wait_timeout <= 0:
                return public_events, next_cursor, done

            pending_event = self._request_waiters.setdefault(
                request_id, asyncio.Event()
            )
            try:
                await asyncio.wait_for(
                    pending_event.wait(), timeout=max(0.0, wait_timeout)
                )
            except asyncio.TimeoutError:
                return [], cursor, False
            self._request_waiters[request_id] = asyncio.Event()

    def schedule_request_execution(self, request_id: str) -> None:
        state = self._requests.get(request_id)
        if state is None:
            state = _RequestState(
                request_id=request_id,
                agent_id=self.app_id,
                updated_event=asyncio.Event(),
            )
            self._requests[request_id] = state
        if state.task is not None and not state.task.done():
            return
        state.task = asyncio.create_task(
            self._execute_request(request_id),
            name=f"MashAgentRequest-{self.app_id}-{request_id}",
        )

    async def _execute_request(self, request_id: str) -> None:
        semaphore = self._semaphore
        if semaphore is None:
            raise RuntimeError("runtime loop is not initialized")
        state = self._requests.get(request_id)
        if state is None:
            raise KeyError(request_id)
        replay_state = await self._workflow_executor.load_state(request_id)
        target_session_id = (
            replay_state.session_id
            or replay_state.initial_session_id
            or self.default_session_id
        )
        session_lock = self._session_locks.get(target_session_id)
        if session_lock is None:
            session_lock = self._increment_session_ref(target_session_id)
        request_token = self._current_request_id.set(request_id)
        try:
            async with semaphore:
                if session_lock.locked():
                    await self.append_runtime_event(
                        RuntimeEvent(
                            request_id=request_id,
                            trace_id=replay_state.trace_id,
                            app_id=self.app_id,
                            agent_id=self.app_id,
                            session_id=target_session_id,
                            event_type=RuntimeEventType.REQUEST_WAITING.value,
                            payload={
                                "reason": "session_busy",
                            },
                        )
                    )

                async with session_lock:
                    try:
                        await self._workflow_executor.run(request_id)
                    except asyncio.CancelledError:
                        await self.append_runtime_event(
                            RuntimeEvent(
                                request_id=request_id,
                                trace_id=replay_state.trace_id,
                                app_id=self.app_id,
                                agent_id=self.app_id,
                                session_id=target_session_id,
                                event_type=RuntimeEventType.REQUEST_FAILED.value,
                                payload={
                                    "request_id": request_id,
                                    "agent_id": self.app_id,
                                    "status": "error",
                                    "session_id": target_session_id,
                                    "error": "request cancelled",
                                    "error_code": "request_cancelled",
                                    "retryable": False,
                                },
                            )
                        )
                        raise
                    except Exception as exc:  # pragma: no cover - defensive
                        await self.append_runtime_event(
                            RuntimeEvent(
                                request_id=request_id,
                                trace_id=replay_state.trace_id,
                                app_id=self.app_id,
                                agent_id=self.app_id,
                                session_id=target_session_id,
                                event_type=RuntimeEventType.REQUEST_FAILED.value,
                                payload={
                                    "request_id": request_id,
                                    "agent_id": self.app_id,
                                    "status": "error",
                                    "session_id": target_session_id,
                                    **classify_error(exc),
                                },
                            )
                        )
        finally:
            self._current_request_id.reset(request_token)
            latest = await self._workflow_executor.load_state(request_id)
            if latest.is_terminal:
                state.done = True
                state.completed_at = time.time()
            waiter = self._request_waiters.get(request_id)
            if waiter is not None:
                waiter.set()
            self._decrement_session_ref(target_session_id)

    async def append_runtime_event(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = await self.runtime_store.append_event(event)
        state = self._requests.get(stored.request_id)
        if state is not None:
            if stored.event_type == RuntimeEventType.TRACE_STARTED.value:
                state.status = "started"
                state.started_at = stored.created_at
            elif stored.event_type in {
                RuntimeEventType.REQUEST_COMPLETED.value,
                RuntimeEventType.REQUEST_FAILED.value,
            }:
                state.status = (
                    "completed"
                    if stored.event_type == RuntimeEventType.REQUEST_COMPLETED.value
                    else "error"
                )
                state.done = True
                state.completed_at = stored.created_at
            self._notify_state_change(state)
        waiter = self._request_waiters.get(stored.request_id)
        if waiter is not None:
            waiter.set()
        return stored

    async def _handle_runtime_event(self, event: Any) -> None:
        del event
        return None

    def _to_public_event(self, event: RuntimeEvent) -> dict[str, Any]:
        if event.event_type == RuntimeEventType.REQUEST_ACCEPTED.value:
            return {
                "event": "request.accepted",
                "data": {
                    "request_id": event.request_id,
                    "agent_id": event.agent_id,
                    "session_id": event.session_id,
                    "status": "accepted",
                },
            }
        if event.event_type == RuntimeEventType.REQUEST_WAITING.value:
            return {
                "event": "request.waiting",
                "data": {
                    "request_id": event.request_id,
                    "agent_id": event.agent_id,
                    "session_id": event.session_id,
                    "status": "waiting",
                    "reason": event.payload.get("reason", "session_busy"),
                },
            }
        if event.event_type == RuntimeEventType.TRACE_STARTED.value:
            return {
                "event": "request.started",
                "data": {
                    "request_id": event.request_id,
                    "agent_id": event.agent_id,
                    "session_id": event.session_id,
                    "status": "started",
                },
            }
        if event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
            return {"event": "request.completed", "data": dict(event.payload or {})}
        if event.event_type == RuntimeEventType.REQUEST_FAILED.value:
            return {"event": "request.error", "data": dict(event.payload or {})}
        return {
            "event": "agent.trace",
            "data": {
                "event_type": event.event_type,
                "trace_id": event.trace_id,
                "loop_index": event.loop_index,
                "step_key": event.step_key,
                "payload": dict(event.payload or {}),
            },
        }

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
            await self.runtime_store.close()
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

"""Agent runtime for orchestrating tool-driven workflows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from anthropic import Anthropic

from .context import CLIContext
from .logging import AgentTraceEvent, DebugEvent, EventLogger
from .memory import Memory
from .telemetry import TelemetryCollector, TokenUsage
from .tools import ToolRegistry, ToolResult, ToolSpec, format_tool_payload

_TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_bm25_20251119"
_TOOL_SEARCH_TOOL_NAME = "tool_search_tool_bm25"
_TOOL_SEARCH_BETAS = ("advanced-tool-use-2025-11-20",)


@dataclass
class AgentConfig:
    """Configuration for agent workflows."""

    app_id: str
    system_prompt: str = ""
    app_context: str = ""
    model: str = ""
    max_steps: int = 4
    max_tokens: int = 1024
    max_history_messages: int = 10
    tool_search_enabled: bool = True
    anthropic_api_key: Optional[str] = None


@dataclass
class ToolCall:
    """Represents a requested tool call."""

    tool_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AgentStep:
    """One step in the agent loop."""

    step_id: int
    tool_calls: List[ToolCall]
    usage: TokenUsage


@dataclass
class AgentReply:
    """Reply payload returned to the caller."""

    text: str
    steps: List[AgentStep]
    usage: TokenUsage
    trace_id: str


class AnthropicProvider:
    """Thin wrapper over the Anthropic client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        event_logger: EventLogger,
        app_id: str,
    ) -> None:
        self._event_logger = event_logger
        self._app_id = app_id
        try:
            self._client = Anthropic(api_key=api_key)
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Anthropic client is not installed. Install `anthropic` to enable agent mode."
            ) from exc

    def create_message(
        self,
        *,
        session_id: str,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        betas: Optional[List[str]] = None,
    ) -> Any:
        event = DebugEvent(
            event_type="llm.request",
            app_id=self._app_id,
            session_id=session_id,
            payload={
                "tools": tools,
            },
        )
        self._event_logger.emit(event)
        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        if betas:
            return self._client.beta.messages.create(**params, betas=betas)
        return self._client.messages.create(**params)


class AgentRuntime:
    """Agent runtime that performs tool-driven orchestration."""

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        memory: Memory,
        event_logger: EventLogger,
        telemetry: TelemetryCollector,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._memory = memory
        self._telemetry = telemetry
        self._event_logger = event_logger
        self._llm = AnthropicProvider(
            api_key=self._config.anthropic_api_key,
            event_logger=event_logger,
            app_id=self._config.app_id,
        )
        self._system_prompt = self._build_system_prompt()
        self._tool_defs: List[Dict[str, Any]] = []
        self._tool_betas: List[str] = []
        self._session_id: Optional[str] = None

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def telemetry(self) -> TelemetryCollector:
        return self._telemetry

    def start_session(self, session_id: str) -> str:
        if self._session_id == session_id and self._system_prompt and self._tool_defs:
            return f"{session_id}-{int(time.time() * 1000)}"
        self._session_id = session_id

        for tool in self._memory_tools(session_id):
            self._tool_registry.register(tool)
        tool_defs = self._tool_registry.to_anthropic_tools(
            enable_search=self._config.tool_search_enabled
        )
        if self._config.tool_search_enabled:
            tool_defs.insert(
                0,
                {"type": _TOOL_SEARCH_TOOL_TYPE, "name": _TOOL_SEARCH_TOOL_NAME},
            )
        self._tool_defs = tool_defs
        self._tool_betas = (
            list(_TOOL_SEARCH_BETAS) if self._config.tool_search_enabled else []
        )
        return f"{session_id}-{int(time.time() * 1000)}"

    def handle_message(
        self,
        session_id: str,
        agent_trace_id: str,
        text: str,
        *,
        ctx: Optional[CLIContext] = None,
    ) -> AgentReply:
        trace_id = agent_trace_id
        step_id = 0
        steps: List[AgentStep] = []
        usage_total = TokenUsage()
        self._emit(
            "agent.start",
            session_id,
            trace_id,
            step_id,
            {"text": text, "trace_id": trace_id},
        )
        history = self._memory.get_conversation(
            self._config.app_id,
            session_id,
            limit=self._config.max_history_messages,
        )
        messages = _build_messages(history)
        if text:
            last = messages[-1] if messages else None
            if (
                last is None
                or last.get("role") != "user"
                or last.get("content") != text
            ):
                messages.append({"role": "user", "content": text})
        system_prompt = self._system_prompt
        tool_defs = list(self._tool_defs)
        while step_id < max(1, self._config.max_steps):
            start = time.time()
            response = self._llm.create_message(
                session_id=session_id,
                model=self._config.model,
                system=system_prompt,
                messages=messages,
                tools=tool_defs,
                max_tokens=self._config.max_tokens,
                betas=self._tool_betas,
            )
            latency_ms = int((time.time() - start) * 1000)
            assistant_text, tool_calls, assistant_blocks = _parse_anthropic_response(
                response
            )
            usage = _extract_usage(response)
            usage_total.add(usage)
            self._telemetry.record_request(session_id, usage)
            self._emit(
                "agent.step",
                session_id,
                trace_id,
                step_id,
                {
                    "tool_calls": [call.name for call in tool_calls],
                    "latency_ms": latency_ms,
                    "assistant_text": assistant_text,
                },
                duration_ms=latency_ms,
            )
            steps.append(AgentStep(step_id=step_id, tool_calls=tool_calls, usage=usage))

            if not tool_calls:
                self._emit(
                    "agent.finish",
                    session_id,
                    trace_id,
                    step_id,
                    {"assistant_text": assistant_text, "usage": usage_total.to_dict()},
                )
                return AgentReply(
                    text=assistant_text,
                    steps=steps,
                    usage=usage_total,
                    trace_id=trace_id,
                )

            messages.append({"role": "assistant", "content": assistant_blocks})
            tool_results: List[Dict[str, Any]] = []
            for call in tool_calls:
                self._emit(
                    "tool.call",
                    session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "arguments": call.arguments},
                )
                tool = self._tool_registry.get(call.name)
                if tool is None:
                    result = ToolResult(
                        name=call.name,
                        content="Unknown tool.",
                        is_error=True,
                    )
                else:
                    result = _invoke_tool(tool, call.name, call.arguments, ctx)
                self._emit(
                    "tool.result",
                    session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "is_error": result.is_error},
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.tool_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            step_id += 1

        fallback_text = "Reached max steps without completing."
        self._emit(
            "agent.finish",
            session_id,
            trace_id,
            step_id,
            {"trace_id": trace_id, "usage": usage_total.to_dict()},
        )
        return AgentReply(
            text=fallback_text,
            steps=steps,
            usage=usage_total,
            trace_id=trace_id,
        )

    def _build_system_prompt(self) -> str:
        tool_search_name = (
            _TOOL_SEARCH_TOOL_NAME if self._config.tool_search_enabled else ""
        )
        parts: List[str] = [
            _default_system_prompt(
                tool_search_name=tool_search_name,
                app_context=self._config.app_context,
            )
        ]
        if self._config.system_prompt:
            parts.append(self._config.system_prompt.strip())
        return "\n\n".join(part for part in parts if part)

    def _memory_tools(self, session_id: str) -> Iterable[ToolSpec]:
        app_id = self._config.app_id

        def _get_full_conversation(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_full_conversation",
        ) -> ToolResult:
            conversation = self._memory.get_conversation(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(conversation),
                conversation,
            )

        def _get_preferences(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_preferences",
        ) -> ToolResult:
            preferences = self._memory.get_preferences(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(preferences),
                preferences,
            )

        def _set_preferences(
            args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "set_preferences",
        ) -> ToolResult:
            if "preferences" not in args:
                return ToolResult(_name, "preferences is required.", is_error=True)
            self._memory.set_preferences(app_id, session_id, args.get("preferences"))
            trace_id = _ctx.agent_trace_id if _ctx else None
            self._emit("preferences.write", session_id, trace_id, 0, {})
            return ToolResult(_name, "ok")

        return [
            ToolSpec(
                name="get_full_conversation",
                description="Return the full conversation history for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_full_conversation,
            ),
            ToolSpec(
                name="get_preferences",
                description="Fetch stored user preferences for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_preferences,
            ),
            ToolSpec(
                name="set_preferences",
                description="Store user preferences for this session.",
                input_schema={
                    "type": "object",
                    "properties": {"preferences": {}},
                    "required": ["preferences"],
                },
                source="memory",
                tags={"memory"},
                invoke=_set_preferences,
            ),
        ]

    def _emit(
        self,
        event_type: str,
        session_id: str,
        trace_id: Optional[str],
        step_id: int,
        payload: Dict[str, Any],
        *,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self._event_logger:
            return
        event = AgentTraceEvent(
            event_type=event_type,
            app_id=self._config.app_id,
            session_id=session_id,
            trace_id=trace_id,
            step_id=step_id,
            payload=payload,
            duration_ms=duration_ms,
        )
        self._event_logger.emit(event)


def _invoke_tool(
    tool: ToolSpec,
    tool_name: str,
    args: Dict[str, Any],
    ctx: Optional[CLIContext],
) -> ToolResult:
    try:
        result = tool.invoke(args, ctx)
    except Exception as exc:
        return ToolResult(
            name=tool_name,
            content=f"Tool error: {exc}",
            raw=exc,
            is_error=True,
        )
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(name=tool_name, content=result)
    if result is None:
        return ToolResult(name=tool_name, content="ok")
    return ToolResult(
        name=tool_name,
        content=format_tool_payload(result),
        raw=result,
    )


def _build_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            continue
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _parse_anthropic_response(
    response: Any,
) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
    content = getattr(response, "content", None)
    if content is None:
        return "", [], []
    if isinstance(content, str):
        return content, [], [{"type": "text", "text": content}]
    tool_calls: List[ToolCall] = []
    text_parts: List[str] = []
    blocks: List[Dict[str, Any]] = []
    for block in content:
        block_type = _block_value(block, "type")
        if block_type == "text":
            text = _block_value(block, "text") or ""
            text_parts.append(text)
            blocks.append({"type": "text", "text": text})
        elif block_type == "tool_use":
            tool_id = _block_value(block, "id")
            name = _block_value(block, "name") or ""
            arguments = _block_value(block, "input") or {}
            tool_calls.append(
                ToolCall(
                    tool_id=str(tool_id), name=str(name), arguments=arguments or {}
                )
            )
            blocks.append(
                {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}
            )
        else:
            blocks.append(_coerce_block_dict(block, block_type))
    return "".join(text_parts).strip(), tool_calls, blocks


def _extract_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", input_tokens)
        output_tokens = usage.get("output_tokens", output_tokens)
    return TokenUsage(
        input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0)
    )


def _default_system_prompt(
    *,
    tool_search_name: str,
    app_context: str = "",
) -> str:
    tool_search_line = ""
    if tool_search_name:
        tool_search_line = (
            f"You also have {tool_search_name} for discovering tools by name "
            "or description when you are unsure what to call. "
            f"Use {tool_search_name} if you are not confident about the right "
            "tool name."
        )
    base_prompt = (
        "You are a Mash Agent running inside a Mash CLI app. "
        "Your job is to understand the user's request, use available tools, "
        "and respond clearly and efficiently. "
        "You have built-in tools get_full_conversation, get_preferences, and "
        "set_preferences for working with session context. "
        "Only store user preferences from the conversation, and save them "
        "with set_preferences. "
        f"{tool_search_line}"
    ).strip()
    parts = [base_prompt]
    if app_context:
        parts.append(app_context.strip())
    return "\n\n".join(part for part in parts if part)


def _coerce_block_dict(block: Any, block_type: Optional[str]) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except TypeError:
            pass
    if hasattr(block, "dict"):
        try:
            return block.dict()
        except TypeError:
            pass
    data = {}
    raw = getattr(block, "__dict__", None)
    if isinstance(raw, dict):
        data.update(raw)
    if block_type:
        data.setdefault("type", block_type)
    if not data:
        data = {"type": block_type or "unknown", "text": str(block)}
    return data


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    value = getattr(block, key, None)
    if value is not None:
        return value
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump().get(key)
        except TypeError:
            pass
    if hasattr(block, "dict"):
        try:
            return block.dict().get(key)
        except TypeError:
            pass
    return None

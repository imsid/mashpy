"""Structured event types for logging."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_ENVELOPE_FIELDS = frozenset({"event_type", "ts", "app_id", "session_id", "event_class"})
_CLASS_FIELDS: dict[str, tuple[str, ...]] = {
    "CommandEvent": ("command_name", "args", "duration_ms", "error", "trace_id"),
    "AgentTraceEvent": (
        "trace_id",
        "step_id",
        "duration_ms",
        "action_type",
        "tool_calls",
        "skill_calls",
        "token_usage",
    ),
    "MCPEvent": (
        "server_name",
        "server_url",
        "tool_name",
        "duration_ms",
        "error",
        "metadata",
        "trace_id",
    ),
    "LLMEvent": (
        "provider",
        "model",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "finish_reason",
        "error",
        "metadata",
        "trace_id",
        "tools",
        "betas",
    ),
    "MemorySearchEvent": (
        "query_id",
        "level",
        "stage",
        "duration_ms",
        "error",
        "metadata",
    ),
    "DebugEvent": (
        "message",
        "exception_type",
        "exception_message",
        "stack_trace",
        "context",
    ),
}


def _normalized_payload(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _require_present(value: Any, field_name: str) -> None:
    if value is None:
        raise ValueError(f"{field_name} is required")


def _require_payload_text(payload: Dict[str, Any], field_name: str) -> None:
    _require_text(payload.get(field_name), f"payload.{field_name}")


def _validate_log_event(raw: Dict[str, Any]) -> None:
    event_type = _require_text(raw.get("event_type"), "event_type")
    _require_text(raw.get("app_id"), "app_id")
    _require_text(raw.get("event_class"), "event_class")

    ts_value = raw.get("ts")
    if ts_value is None:
        raise ValueError("ts is required")
    try:
        float(ts_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ts is required") from exc

    event_class = str(raw.get("event_class"))
    nested_payload = _normalized_payload(raw.get("payload"))

    if event_class == "AgentTraceEvent":
        _require_text(raw.get("trace_id"), "trace_id")
    elif event_class == "LLMEvent":
        _require_text(raw.get("trace_id"), "trace_id")
        _require_text(raw.get("provider"), "provider")
        _require_text(raw.get("model"), "model")
    elif event_class == "MemorySearchEvent":
        _require_text(raw.get("query_id"), "query_id")
        _require_text(raw.get("level"), "level")
        _require_text(raw.get("stage"), "stage")
    elif event_class == "CommandEvent" and event_type.startswith("command."):
        _require_text(raw.get("command_name"), "command_name")
    elif event_class == "MCPEvent":
        _require_text(raw.get("server_name"), "server_name")

    if event_type == "agent.step.complete":
        _require_present(raw.get("step_id"), "step_id")
        _require_present(raw.get("duration_ms"), "duration_ms")
        _require_text(raw.get("action_type"), "action_type")
    elif event_type in {"agent.think.complete", "agent.act.complete"}:
        _require_present(raw.get("duration_ms"), "duration_ms")
        _require_text(raw.get("action_type"), "action_type")
    elif event_type == "agent.tool.call":
        _require_payload_text(nested_payload, "tool_name")
        _require_payload_text(nested_payload, "tool_call_id")
    elif event_type == "agent.tool.result":
        _require_payload_text(nested_payload, "tool_name")
        _require_payload_text(nested_payload, "tool_call_id")
        if "is_error" not in nested_payload:
            raise ValueError("payload.is_error is required")
    elif event_type == "mcp.tool.call":
        _require_text(raw.get("tool_name"), "tool_name")
    elif event_type == "mcp.tool.result":
        _require_text(raw.get("tool_name"), "tool_name")
        _require_present(raw.get("duration_ms"), "duration_ms")
    elif event_type == "mcp.tool.error":
        _require_text(raw.get("tool_name"), "tool_name")
        _require_present(raw.get("duration_ms"), "duration_ms")
        _require_text(raw.get("error"), "error")
    elif event_type in {"llm.request.complete", "llm.request.error"}:
        _require_present(raw.get("duration_ms"), "duration_ms")
        if event_type == "llm.request.error":
            _require_text(raw.get("error"), "error")
    elif event_type.startswith("memory.search.") and event_type.endswith(".error"):
        _require_text(raw.get("error"), "error")


def normalize_log_event(event: "LogEvent") -> Dict[str, Any]:
    """Validate one event and convert it to a persisted log record."""
    raw = event.to_dict()
    _validate_log_event(raw)

    event_class = str(raw["event_class"])
    raw_payload = _normalized_payload(raw.get("payload"))
    trace_id_value = raw.get("trace_id")
    trace_id = trace_id_value.strip() if isinstance(trace_id_value, str) and trace_id_value.strip() else None

    payload: Dict[str, Any] = {"payload": raw_payload}
    for field_name in _CLASS_FIELDS.get(event_class, ()):
        if field_name == "trace_id":
            continue
        value = raw.get(field_name)
        if value is not None:
            payload[field_name] = value

    known_fields = _ENVELOPE_FIELDS.union(_CLASS_FIELDS.get(event_class, ()))
    for field_name, value in raw.items():
        if field_name in known_fields or field_name == "payload":
            continue
        if value is not None:
            payload[field_name] = value

    return {
        "app_id": str(raw["app_id"]),
        "session_id": raw.get("session_id"),
        "trace_id": trace_id,
        "event_class": event_class,
        "event_type": str(raw["event_type"]),
        "created_at": float(raw["ts"]),
        "payload": payload,
    }


def inflate_logged_event(
    *,
    log_id: Optional[int],
    app_id: str,
    session_id: Optional[str],
    trace_id: Optional[str],
    event_class: str,
    event_type: str,
    created_at: float,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Reconstruct the public event shape from a stored DB row."""
    restored: Dict[str, Any] = {
        "event_type": event_type,
        "ts": float(created_at),
        "app_id": app_id,
        "session_id": session_id,
        "event_class": event_class,
        "payload": _normalized_payload(payload.get("payload")),
    }
    if log_id is not None:
        restored["log_id"] = int(log_id)

    for field_name in _CLASS_FIELDS.get(event_class, ()):
        if field_name == "trace_id":
            restored[field_name] = trace_id
        else:
            restored[field_name] = payload.get(field_name)

    known_fields = set(_CLASS_FIELDS.get(event_class, ()))
    for field_name, value in payload.items():
        if field_name == "payload" or field_name in known_fields:
            continue
        restored[field_name] = value

    return restored


@dataclass(frozen=True)
class LogEvent:
    """Base event written to the log destination.

    All events share these common fields:
    - event_type: Categorizes the event (e.g., "command.start", "agent.think")
    - app_id: Application identifier
    - session_id: Session identifier for grouping related events
    - payload: Additional event-specific data
    - ts: Unix timestamp when event was created
    """

    event_type: str
    app_id: str
    session_id: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary for JSON encoding."""
        return {
            "event_type": self.event_type,
            "ts": self.ts,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "event_class": type(self).__name__,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CommandEvent(LogEvent):
    """Event emitted for command execution lifecycle.

    Tracks user commands from invocation to completion.

    Example event_types:
    - "command.start" - User invoked a command
    - "command.complete" - Command finished successfully
    - "command.error" - Command failed with error

    Additional fields:
    - command_name: Name of the command (e.g., "/help", "/switch")
    - args: Command arguments
    - duration_ms: How long command took to execute
    - error: Error message if command failed
    - trace_id: Trace ID if command is part of an agent execution
    """

    command_name: Optional[str] = None
    args: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "command_name": self.command_name,
                "args": self.args,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "trace_id": self.trace_id,
            }
        )
        return payload


@dataclass(frozen=True)
class AgentTraceEvent(LogEvent):
    """Structured event emitted during agent execution.

    Tracks the agent's execution lifecycle and correlated subagent mirrors.

    Example event_types:
    - "agent.run.start" - Agent execution started
    - "agent.run.complete" - Agent execution finished
    - "agent.think.start" - Agent starting to think
    - "agent.think.complete" - Agent decided on action
    - "agent.act.complete" - Tools executed
    - "agent.step.complete" - Full step completed
    - "agent.tool.call" - Tool invoked
    - "agent.tool.result" - Tool returned
    - "subagent.request.*" - Mirrored child request lifecycle in the parent trace
    - "subagent.agent.trace" - Mirrored child trace event in the parent trace

    Additional fields:
    - trace_id: Unique identifier for this execution trace
    - step_id: Step number in the agent loop
    - duration_ms: How long this operation took
    - action_type: Type of action taken (e.g., "tool_call", "response", "finish")
    - tool_calls: List of tools called
    - token_usage: Token counts from LLM
    """

    trace_id: Optional[str] = None
    step_id: Optional[int] = None
    duration_ms: Optional[int] = None
    action_type: Optional[str] = None
    tool_calls: Optional[List[str]] = None
    skill_calls: Optional[List[str]] = None
    token_usage: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "trace_id": self.trace_id,
                "step_id": self.step_id,
                "duration_ms": self.duration_ms,
                "action_type": self.action_type,
                "tool_calls": self.tool_calls,
                "skill_calls": self.skill_calls,
                "token_usage": self.token_usage,
            }
        )
        return payload


@dataclass(frozen=True)
class MCPEvent(LogEvent):
    """Event emitted for MCP client and host operations.

    Tracks MCP server connections and MCP tool calls.

    Example event_types:
    - "mcp.client.connect" - Connecting to MCP server
    - "mcp.client.connected" - Successfully connected
    - "mcp.client.disconnect" - Client disconnected
    - "mcp.client.error" - Client connection or lifecycle error
    - "mcp.tool.call" - Tool invoked via MCP
    - "mcp.tool.result" - Tool execution completed
    - "mcp.tool.error" - Tool execution failed

    Additional fields:
    - server_name: MCP server identifier
    - server_url: Server URL/connection string
    - tool_name: Name of tool being called
    - duration_ms: Operation duration
    - error: Error message if operation failed
    - metadata: Additional server/tool specific data
    - trace_id: Trace ID if tool call is part of an agent execution
    """

    server_name: Optional[str] = None
    server_url: Optional[str] = None
    tool_name: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "server_name": self.server_name,
                "server_url": self.server_url,
                "tool_name": self.tool_name,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "metadata": self.metadata,
                "trace_id": self.trace_id,
            }
        )
        return payload


@dataclass(frozen=True)
class LLMEvent(LogEvent):
    """Event emitted for LLM provider operations.

    Tracks LLM request lifecycle, token usage, and provider metadata.

    Example event_types:
    - "llm.request.start" - Starting LLM API call
    - "llm.request.complete" - LLM responded successfully
    - "llm.request.error" - LLM request failed

    Additional fields:
    - provider: LLM provider name (e.g., "anthropic", "openai")
    - model: Model identifier (e.g., "claude-3-5-sonnet-20241022")
    - duration_ms: Request duration
    - input_tokens: Tokens in the prompt
    - output_tokens: Tokens in the response
    - total_tokens: Total tokens used
    - cache_creation_input_tokens: Tokens written to cache (first request)
    - cache_read_input_tokens: Tokens read from cache (subsequent requests)
    - finish_reason: Why the response ended (e.g., "end_turn", "max_tokens")
    - error: Error message if request failed
    - metadata: Additional provider-specific data
    - trace_id: Trace ID for correlating with agent execution
    - tools: List of tool names available for this request
    - betas: List of beta feature flags used in this request
    """

    provider: Optional[str] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    tools: Optional[List[str]] = None
    betas: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "provider": self.provider,
                "model": self.model,
                "duration_ms": self.duration_ms,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "finish_reason": self.finish_reason,
                "error": self.error,
                "metadata": self.metadata,
                "trace_id": self.trace_id,
                "tools": self.tools,
                "betas": self.betas,
            }
        )
        return payload


@dataclass(frozen=True)
class MemorySearchEvent(LogEvent):
    """Event emitted for memory search pipeline operations.

    Tracks parse/retrieval/rerank lifecycle when `MemorySearchService.search()`
    is invoked.

    Example event_types:
    - "memory.search.start"
    - "memory.search.parse.complete"
    - "memory.search.parse.error"
    - "memory.search.retrieval.complete"
    - "memory.search.retrieval.error"
    - "memory.search.rerank.complete"
    - "memory.search.rerank.error"
    - "memory.search.complete"

    Notes:
    - `memory.search.start` and `memory.search.complete` are service-level events.
    - Errors are emitted at the stage that failed, not as one top-level
      `memory.search.error` event.
    """

    query_id: Optional[str] = None
    level: Optional[str] = None
    stage: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "query_id": self.query_id,
                "level": self.level,
                "stage": self.stage,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "metadata": self.metadata,
            }
        )
        return payload


@dataclass(frozen=True)
class DebugEvent(LogEvent):
    """Free-form debug event for debugging and exception tracking.

    Use this for logging exceptions, debugging information, and other
    unstructured diagnostic data.

    Example event_types:
    - "debug.exception" - Caught exception
    - "debug.warning" - Warning condition
    - "debug.info" - Informational debug message
    - "debug.trace" - Detailed trace information

    Additional fields:
    - message: Debug message
    - exception_type: Type of exception (if applicable)
    - exception_message: Exception message
    - stack_trace: Stack trace string
    - context: Additional debug context
    """

    message: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    stack_trace: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "message": self.message,
                "exception_type": self.exception_type,
                "exception_message": self.exception_message,
                "stack_trace": self.stack_trace,
                "context": self.context,
            }
        )
        return payload

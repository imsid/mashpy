"""Structured event types for logging."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_ENVELOPE_FIELDS = frozenset({"event_type", "ts", "app_id", "session_id", "event_class"})
_CLASS_FIELDS: dict[str, tuple[str, ...]] = {
    "CommandEvent": ("command_name", "args", "duration_ms", "error", "trace_id"),
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
}


def _normalized_payload(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


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

    if event_class == "LLMEvent":
        _require_text(raw.get("trace_id"), "trace_id")
        _require_text(raw.get("provider"), "provider")
        _require_text(raw.get("model"), "model")
    elif event_class == "CommandEvent" and event_type.startswith("command."):
        _require_text(raw.get("command_name"), "command_name")
    elif event_class == "MCPEvent":
        _require_text(raw.get("server_name"), "server_name")


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
    - event_type: Categorizes the event (e.g., "command.start", "chat.delta")
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
    # Stored on the event row, never in its payload: a replayed emit with the
    # same key inside the same request writes nothing new.
    dedupe_key: Optional[str] = None

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
class MCPEvent(LogEvent):
    """Event emitted for MCP client lifecycle operations.

    Tracks MCP server connections; the tool calls themselves are the
    request's ``execute_tool`` events.

    Example event_types:
    - "mcp.client.connect" - Connecting to MCP server
    - "mcp.client.connected" - Successfully connected
    - "mcp.client.disconnect" - Client disconnected
    - "mcp.client.error" - Client connection or lifecycle error

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
    """Event emitted by an LLM provider: one streamed chunk of a model call.

    Event type:
    - "chat.delta" - Incremental streamed text chunk (coalesced); the chunk
      text and ordinal live in payload as {"text", "index"}. The model call's
      outcome, usage and model are on the request's ``chat.end``.

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

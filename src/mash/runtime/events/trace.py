"""Canonical runtime trace parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .types import RuntimeEvent, RuntimeEventType


@dataclass(frozen=True)
class RuntimeTrace:
    """Parsed projection of one runtime trace."""

    target_agent_id: str
    session_id: str
    trace_id: str
    events: list[dict[str, Any]]
    started_at: float
    latest_event_at: float
    duration_ms: float
    status: str
    user_message: str
    assistant_response: str
    tools_called: list[str]
    tool_call_count: int
    tool_error_count: int
    step_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    failed_events: list[dict[str, Any]]
    reasoning_steps: list[dict[str, Any]]
    reasoning_error: dict[str, Any] | None = None

    def to_reasoning_trace_payload(self) -> dict[str, Any]:
        total_tools = 0
        total_tokens = 0
        total_duration_ms = 0
        for step in self.reasoning_steps:
            total_tools += len(step.get("tool_calls") or [])
            token_usage = _dict_value(step.get("token_usage"))
            total_tokens += int(token_usage.get("input", 0) or 0) + int(
                token_usage.get("output", 0) or 0
            )
            total_duration_ms += _reasoning_step_duration_ms(step)

        trace = {
            "status": self.status,
            "steps": self.reasoning_steps,
            "summary": {
                "total_steps": len(self.reasoning_steps),
                "total_tools": total_tools,
                "total_tokens": total_tokens,
                "total_duration_ms": total_duration_ms,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
            },
        }
        if self.reasoning_error is not None:
            trace["error"] = self.reasoning_error
        return trace


def serialize_runtime_event(event: RuntimeEvent) -> dict[str, Any]:
    """Serialize a runtime event into the public telemetry/event shape."""
    return {
        "event_id": int(event.event_id),
        "request_id": event.request_id,
        "request_seq": event.request_seq,
        "trace_id": event.trace_id,
        "app_id": event.app_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "host_id": event.host_id,
        "workflow_id": event.workflow_id,
        "workflow_run_id": event.workflow_run_id,
        "event_type": event.event_type,
        "loop_index": event.loop_index,
        "step_key": event.step_key,
        "payload": dict(event.payload or {}),
        "created_at": float(event.created_at),
    }


def build_runtime_trace(events: list[RuntimeEvent]) -> RuntimeTrace:
    """Build the canonical parsed runtime trace projection."""
    serialized = [serialize_runtime_event(event) for event in events]
    if serialized:
        started_at = min(float(event["created_at"]) for event in serialized)
        latest_event_at = max(float(event["created_at"]) for event in serialized)
    else:
        started_at = 0.0
        latest_event_at = 0.0

    failed_events = _failed_events(serialized)
    token_usage = _sum_token_usage(serialized)
    tool_events = _tool_events(serialized)
    reasoning_steps, reasoning_status, reasoning_error = _build_reasoning_steps(events)
    target_agent_id = _first_text(
        [event.app_id for event in events] + [event.agent_id for event in events]
    )
    session_id = _first_text(event.session_id for event in events)
    trace_id = _first_text(event.trace_id for event in events)

    return RuntimeTrace(
        target_agent_id=target_agent_id,
        session_id=session_id,
        trace_id=trace_id,
        events=serialized,
        started_at=started_at,
        latest_event_at=latest_event_at,
        duration_ms=round((latest_event_at - started_at) * 1000.0, 3),
        status=reasoning_status,
        user_message=_extract_user_message(serialized),
        assistant_response=_extract_assistant_response(serialized),
        tools_called=_tools_called(serialized),
        tool_call_count=len(tool_events),
        tool_error_count=len([event for event in tool_events if _is_failed_event(event)]),
        step_count=_step_count(serialized),
        input_tokens=token_usage["input_tokens"],
        output_tokens=token_usage["output_tokens"],
        cache_read_tokens=token_usage["cache_read_tokens"],
        cache_write_tokens=token_usage["cache_write_tokens"],
        failed_events=failed_events,
        reasoning_steps=reasoning_steps,
        reasoning_error=reasoning_error,
    )


def build_reasoning_trace(events: list[RuntimeEvent]) -> dict[str, Any]:
    """Build a compact reasoning trace from raw runtime events."""
    return build_runtime_trace(events).to_reasoning_trace_payload()


def runtime_event_from_stream_payload(
    payload: Mapping[str, Any],
    *,
    app_id: str,
    agent_id: str | None = None,
) -> RuntimeEvent | None:
    """Hydrate one streamed agent.trace payload into a RuntimeEvent."""
    nested = payload.get("payload")
    if not isinstance(nested, Mapping):
        return None
    event_type = _clean_text(payload.get("event_type"))
    if event_type is None:
        return None
    return RuntimeEvent(
        event_id=_as_int(payload.get("event_id")) or 0,
        request_id=_clean_text(payload.get("request_id")),
        request_seq=_as_int(payload.get("request_seq")),
        trace_id=_clean_text(payload.get("trace_id")),
        app_id=str(app_id or ""),
        agent_id=str(agent_id or app_id or ""),
        session_id=_clean_text(payload.get("session_id")),
        event_type=event_type,
        loop_index=_as_int(payload.get("loop_index")),
        step_key=_clean_text(payload.get("step_key")),
        payload=dict(nested),
        created_at=float(payload.get("created_at") or 0.0),
    )


def runtime_event_response_preview(event: RuntimeEvent) -> str:
    """Return streamed assistant preview text for one runtime event."""
    if event.event_type != RuntimeEventType.LLM_THINK_COMPLETED.value:
        return ""
    if str((event.payload or {}).get("action_type") or "") != "response":
        return ""
    text = _clean_text((event.payload or {}).get("assistant_text"))
    return text or ""


def _build_reasoning_steps(
    events: list[RuntimeEvent],
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
    steps: list[dict[str, Any]] = []
    by_step_index: dict[int, dict[str, Any]] = {}
    status = "in_progress"
    error: dict[str, Any] | None = None

    for event in events:
        if event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
            step = _reasoning_step_from_runtime_event(event, display_step=len(steps) + 1)
            steps.append(step)
            if event.loop_index is not None:
                by_step_index[int(event.loop_index)] = step
            continue

        if event.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        }:
            step = _resolve_reasoning_step(event.loop_index, steps, by_step_index)
            if step is None:
                continue
            duration_ms = _as_int((event.payload or {}).get("duration_ms"))
            if duration_ms is not None:
                current = _as_int(step.get("act_duration_ms")) or 0
                step["act_duration_ms"] = current + duration_ms
            continue

        if event.event_type == RuntimeEventType.STEP_COMPLETED.value:
            step = _resolve_reasoning_step(event.loop_index, steps, by_step_index)
            if step is None:
                continue
            duration_ms = _as_int((event.payload or {}).get("duration_ms"))
            if duration_ms is not None:
                step["total_duration_ms"] = duration_ms
            continue

        if event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
            status = "completed"
            continue

        if event.event_type == RuntimeEventType.REQUEST_FAILED.value:
            status = "error"
            error = {
                "message": _clean_text((event.payload or {}).get("error")),
                "error_type": _clean_text((event.payload or {}).get("error_type")),
            }

    return steps, status, error


def _reasoning_step_from_runtime_event(
    event: RuntimeEvent,
    *,
    display_step: int,
) -> dict[str, Any]:
    payload = dict(event.payload or {})
    action_type = _clean_text(payload.get("action_type")) or "unknown"
    tool_calls_detail = [
        _tool_call_entry(tool_call)
        for tool_call in _tool_calls_detail(payload)
    ]
    return {
        "step": display_step,
        "step_index": int(event.loop_index) if event.loop_index is not None else None,
        "action_type": action_type,
        "title": _reasoning_title(action_type, tool_calls_detail),
        "assistant_text": _clean_text(payload.get("assistant_text")),
        "tool_calls": tool_calls_detail,
        "token_usage": _dict_value(payload.get("token_usage")),
        "think_duration_ms": _as_int(payload.get("duration_ms")),
        "act_duration_ms": None,
        "total_duration_ms": None,
    }


def _resolve_reasoning_step(
    step_index: int | None,
    steps: list[dict[str, Any]],
    by_step_index: Mapping[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if step_index is not None:
        step = by_step_index.get(int(step_index))
        if step is not None:
            return step
    if steps:
        return steps[-1]
    return None


def _reasoning_step_duration_ms(step: Mapping[str, Any]) -> int:
    total_duration = _as_int(step.get("total_duration_ms"))
    if total_duration is not None:
        return total_duration
    think_duration = _as_int(step.get("think_duration_ms")) or 0
    act_duration = _as_int(step.get("act_duration_ms")) or 0
    return think_duration + act_duration


def _reasoning_title(
    action_type: str,
    tool_calls: list[dict[str, Any]],
) -> str:
    tool_names = [str(item.get("name") or "").strip() for item in tool_calls]
    tool_names = [name for name in tool_names if name]
    if action_type == "tool_call" and tool_names:
        return f"Calling tools: {', '.join(tool_names)}"
    if action_type == "response":
        return "Generating response"
    if action_type == "finish":
        return "Finishing execution"
    return f"Action: {action_type}"


def _tool_call_entry(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    name = _clean_text(tool_call.get("name")) or "unknown"
    arguments = _dict_value(tool_call.get("arguments"))
    return {
        "name": name,
        "arguments": arguments,
        "preview": _tool_call_preview(name, arguments),
    }


def _tool_call_preview(tool_name: str, tool_args: Mapping[str, Any]) -> str:
    if tool_name == "bash" and "command" in tool_args:
        command = str(tool_args.get("command") or "")
        if len(command) > 80:
            command = command[:77] + "..."
        return f"$ {command}"
    if not tool_args:
        return tool_name
    args_preview: list[str] = []
    for key, value in list(tool_args.items())[:2]:
        display_value = value
        if isinstance(display_value, str) and len(display_value) > 40:
            display_value = display_value[:37] + "..."
        args_preview.append(f"{key}={display_value}")
    if len(tool_args) > 2:
        args_preview.append(f"+{len(tool_args) - 2} more")
    return f"{tool_name}({', '.join(args_preview)})"


def _tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime_tool_events = [
        event
        for event in events
        if event.get("event_type")
        in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        }
    ]
    if runtime_tool_events:
        return runtime_tool_events
    return [
        event
        for event in events
        if ".tool." in str(event.get("event_type") or "")
        or str((event.get("payload") or {}).get("tool_name") or "")
    ]


def _failed_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if _is_failed_event(event)]


def _is_failed_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    if event_type in {
        RuntimeEventType.REQUEST_FAILED.value,
        RuntimeEventType.STEP_FAILED.value,
    }:
        return True
    lowered = event_type.lower()
    payload = event.get("payload") or {}
    return (
        "error" in lowered
        or "fail" in lowered
        or str(payload.get("status") or "").lower() in {"error", "failed"}
    )


def _tools_called(events: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for event in _tool_events(events):
        payload = event.get("payload") or {}
        for key in ("tool_name", "tool", "name", "command_name"):
            raw = payload.get(key)
            if isinstance(raw, str):
                name = raw.strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


def _step_count(events: list[dict[str, Any]]) -> int:
    step_events = [
        event
        for event in events
        if str(event.get("event_type") or "") == RuntimeEventType.STEP_COMPLETED.value
    ]
    indexes = {
        int(event["loop_index"])
        for event in step_events
        if event.get("loop_index") is not None
    }
    if indexes:
        return len(indexes)
    return len(step_events)


def _extract_user_message(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("event_type") != RuntimeEventType.TRACE_STARTED.value:
            continue
        raw = (event.get("payload") or {}).get("message")
        if isinstance(raw, str) and raw:
            return raw
    for event in events:
        if event.get("event_type") != "agent.run.start":
            continue
        raw = (event.get("payload") or {}).get("user_message")
        if isinstance(raw, str) and raw:
            return raw
    for event in events:
        raw = (event.get("payload") or {}).get("message")
        if isinstance(raw, str) and raw:
            return raw
    return ""


def _extract_assistant_response(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("event_type") != RuntimeEventType.REQUEST_COMPLETED.value:
            continue
        response = (event.get("payload") or {}).get("response")
        if isinstance(response, dict):
            text = response.get("text")
            if isinstance(text, str) and text:
                return text
    for event in reversed(events):
        if event.get("event_type") != "agent.run.complete":
            continue
        raw = (event.get("payload") or {}).get("assistant_response")
        if isinstance(raw, str) and raw:
            return raw
    for event in reversed(events):
        payload = event.get("payload") or {}
        raw = payload.get("agent_response") or payload.get("assistant_response")
        if isinstance(raw, str) and raw:
            return raw
    return ""


def _sum_token_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    for event in events:
        if event.get("event_type") != RuntimeEventType.LLM_THINK_COMPLETED.value:
            continue
        payload = event.get("payload") or {}
        usage = payload.get("token_usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += _safe_int(usage.get("input") or usage.get("input_tokens"))
        output_tokens += _safe_int(usage.get("output") or usage.get("output_tokens"))
        cache_read_tokens += _safe_int(usage.get("cache_read") or usage.get("cache_read_tokens"))
        cache_write_tokens += _safe_int(usage.get("cache_write") or usage.get("cache_write_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tool_calls_detail(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _tool_call_names(raw: Any) -> list[str]:
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
            else:
                name = ""
            if name:
                names.append(name)
    return names


def _first_text(values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

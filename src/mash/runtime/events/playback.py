"""Helpers for reasoning traces and renderer payloads from runtime events."""

from __future__ import annotations

from typing import Any, Mapping

from .types import RuntimeEvent, RuntimeEventType


def build_reasoning_trace(events: list[RuntimeEvent]) -> dict[str, Any]:
    """Build a compact reasoning trace from raw runtime events."""
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

    total_tools = 0
    total_tokens = 0
    total_duration_ms = 0
    for step in steps:
        total_tools += len(step.get("tool_calls") or [])
        token_usage = _dict_value(step.get("token_usage"))
        total_tokens += int(token_usage.get("input", 0) or 0) + int(
            token_usage.get("output", 0) or 0
        )
        total_duration_ms += _reasoning_step_duration_ms(step)

    trace = {
        "status": status,
        "steps": steps,
        "summary": {
            "total_steps": len(steps),
            "total_tools": total_tools,
            "total_tokens": total_tokens,
            "total_duration_ms": total_duration_ms,
        },
    }
    if error is not None:
        trace["error"] = error
    return trace


def runtime_event_to_trace_payload(
    event: RuntimeEvent,
    *,
    trace_label: str | None = None,
) -> dict[str, Any] | None:
    """Convert one raw runtime event into a renderer-oriented trace payload."""
    return _event_payload_to_trace_payload(
        event_type=event.event_type,
        trace_id=event.trace_id,
        step_id=event.loop_index,
        payload=event.payload,
        trace_label=trace_label,
    )


def runtime_trace_payload_to_trace_payload(
    payload: Mapping[str, Any],
    *,
    trace_label: str | None = None,
) -> dict[str, Any] | None:
    """Convert one streamed raw runtime trace payload into renderer payload."""
    nested = payload.get("payload")
    if not isinstance(nested, Mapping):
        return None
    return _event_payload_to_trace_payload(
        event_type=str(payload.get("event_type") or ""),
        trace_id=_clean_text(payload.get("trace_id")),
        step_id=_as_int(payload.get("loop_index")),
        payload=dict(nested),
        trace_label=trace_label,
    )


def runtime_trace_payload_response_preview(payload: Mapping[str, Any]) -> str:
    """Return streamed assistant preview text for one raw runtime trace payload."""
    nested = payload.get("payload")
    if not isinstance(nested, Mapping):
        return ""
    if str(payload.get("event_type") or "") != RuntimeEventType.LLM_THINK_COMPLETED.value:
        return ""
    if str(nested.get("action_type") or "") != "response":
        return ""
    text = _clean_text(nested.get("assistant_text"))
    return text or ""


def _event_payload_to_trace_payload(
    *,
    event_type: str,
    trace_id: str | None,
    step_id: int | None,
    payload: Mapping[str, Any] | None,
    trace_label: str | None,
) -> dict[str, Any] | None:
    nested = dict(payload or {})
    duration_ms = _as_int(nested.get("duration_ms"))

    if event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
        tool_calls_detail = _tool_calls_detail(nested)
        trace_payload = {
            "assistant_text": _clean_text(nested.get("assistant_text")),
            "tool_calls_detail": tool_calls_detail,
        }
        if trace_label:
            trace_payload["trace_label"] = trace_label
        return {
            "event_type": "agent.think.complete",
            "trace_id": trace_id,
            "step_id": step_id,
            "duration_ms": duration_ms,
            "action_type": _clean_text(nested.get("action_type")),
            "tool_calls": _tool_call_names(tool_calls_detail),
            "token_usage": _dict_value(nested.get("token_usage")),
            "payload": trace_payload,
        }

    if event_type in {
        RuntimeEventType.TOOL_CALL_COMPLETED.value,
        RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
    }:
        tool_name = _clean_text(nested.get("tool_name"))
        return {
            "event_type": "agent.act.complete",
            "trace_id": trace_id,
            "step_id": step_id,
            "duration_ms": duration_ms,
            "action_type": (
                "subagent_call"
                if event_type == RuntimeEventType.SUBAGENT_CALL_COMPLETED.value
                else "tool_call"
            ),
            "tool_calls": [tool_name] if tool_name else [],
            "payload": {},
        }

    if event_type == RuntimeEventType.STEP_COMPLETED.value:
        return {
            "event_type": "agent.step.complete",
            "trace_id": trace_id,
            "step_id": step_id,
            "duration_ms": duration_ms,
            "action_type": _clean_text(nested.get("action_type")),
            "tool_calls": _tool_call_names(nested.get("tool_calls")),
            "payload": {},
        }

    return None


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

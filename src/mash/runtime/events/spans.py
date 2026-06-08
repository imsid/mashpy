"""Span model and tree builder for runtime event traces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .types import RuntimeEvent, RuntimeEventType


class SpanKind(str, Enum):
    TRACE = "trace"
    COLD_START = "cold_start"
    CONTEXT_LOAD = "context_load"
    STEP = "step"
    THINK = "think"
    TOOL_CALL = "tool_call"
    SUBAGENT_CALL = "subagent_call"


@dataclass(frozen=True)
class Span:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    kind: SpanKind
    name: str
    start_time: float
    end_time: float
    duration_ms: float
    loop_index: int | None
    attributes: dict[str, Any]
    children: tuple["Span", ...]
    status: str


@dataclass(frozen=True)
class TraceSpanTree:
    trace_id: str
    target_agent_id: str
    session_id: str
    root: Span
    spans_by_id: dict[str, Span]
    span_count: int


def build_span_tree(events: list[RuntimeEvent]) -> TraceSpanTree:
    if not events:
        empty_root = Span(
            span_id="trace:unknown:root",
            trace_id="unknown",
            parent_span_id=None,
            kind=SpanKind.TRACE,
            name="Trace",
            start_time=0.0,
            end_time=0.0,
            duration_ms=0.0,
            loop_index=None,
            attributes={},
            children=(),
            status="error",
        )
        return TraceSpanTree(
            trace_id="unknown",
            target_agent_id="unknown",
            session_id="unknown",
            root=empty_root,
            spans_by_id={empty_root.span_id: empty_root},
            span_count=1,
        )

    sorted_events = sorted(events, key=lambda e: (float(e.created_at), int(e.event_id)))

    trace_id = _first_text(e.trace_id for e in sorted_events) or "unknown"
    target_agent_id = _first_text(e.app_id for e in sorted_events) or "unknown"
    session_id = _first_text(e.session_id for e in sorted_events) or "unknown"

    boundary = _extract_boundary_events(sorted_events)
    root_span_id = f"trace:{trace_id}:root"

    trace_start = boundary["trace_start"]
    trace_end = boundary["trace_end"]
    status = boundary["status"]

    children: list[Span] = []
    all_spans: dict[str, Span] = {}

    cold_start = _build_cold_start_span(trace_id, root_span_id, boundary)
    if cold_start is not None:
        children.append(cold_start)
        all_spans[cold_start.span_id] = cold_start

    context_load = _build_context_load_span(trace_id, root_span_id, boundary)
    if context_load is not None:
        children.append(context_load)
        all_spans[context_load.span_id] = context_load

    step_events = _group_by_step(sorted_events)
    for loop_index in sorted(step_events.keys()):
        step_span = _build_step_span(
            trace_id, root_span_id, loop_index, step_events[loop_index],
        )
        children.append(step_span)
        all_spans[step_span.span_id] = step_span
        for child in step_span.children:
            all_spans[child.span_id] = child

    root = Span(
        span_id=root_span_id,
        trace_id=trace_id,
        parent_span_id=None,
        kind=SpanKind.TRACE,
        name="Trace",
        start_time=trace_start,
        end_time=trace_end,
        duration_ms=round((trace_end - trace_start) * 1000.0, 3),
        loop_index=None,
        attributes={},
        children=tuple(children),
        status=status,
    )
    all_spans[root.span_id] = root

    return TraceSpanTree(
        trace_id=trace_id,
        target_agent_id=target_agent_id,
        session_id=session_id,
        root=root,
        spans_by_id=all_spans,
        span_count=len(all_spans),
    )


def _extract_boundary_events(events: list[RuntimeEvent]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "request_accepted_at": None,
        "trace_started_at": None,
        "context_loaded_at": None,
        "request_completed_at": None,
        "trace_start": float(events[0].created_at),
        "trace_end": float(events[-1].created_at),
        "status": "in_progress",
    }
    for event in events:
        et = event.event_type
        ts = float(event.created_at)
        if et == RuntimeEventType.REQUEST_ACCEPTED.value:
            result["request_accepted_at"] = ts
            result["trace_start"] = min(result["trace_start"], ts)
        elif et == RuntimeEventType.TRACE_STARTED.value:
            result["trace_started_at"] = ts
        elif et == RuntimeEventType.CONTEXT_LOADED.value:
            result["context_loaded_at"] = ts
        elif et == RuntimeEventType.REQUEST_COMPLETED.value:
            result["request_completed_at"] = ts
            result["trace_end"] = max(result["trace_end"], ts)
            result["status"] = "completed"
        elif et == RuntimeEventType.REQUEST_FAILED.value:
            result["request_completed_at"] = ts
            result["trace_end"] = max(result["trace_end"], ts)
            result["status"] = "error"
    return result


def _build_cold_start_span(
    trace_id: str,
    parent_span_id: str,
    boundary: dict[str, Any],
) -> Span | None:
    start = boundary["request_accepted_at"]
    end = boundary["trace_started_at"] or boundary["context_loaded_at"]
    if start is None or end is None or end <= start:
        return None
    duration_ms = round((end - start) * 1000.0, 3)
    if duration_ms < 0.001:
        return None
    return Span(
        span_id=f"cold_start:{trace_id}",
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        kind=SpanKind.COLD_START,
        name="Cold Start",
        start_time=start,
        end_time=end,
        duration_ms=duration_ms,
        loop_index=None,
        attributes={},
        children=(),
        status="ok",
    )


def _build_context_load_span(
    trace_id: str,
    parent_span_id: str,
    boundary: dict[str, Any],
) -> Span | None:
    start = boundary["trace_started_at"]
    end = boundary["context_loaded_at"]
    if start is None or end is None or end <= start:
        return None
    duration_ms = round((end - start) * 1000.0, 3)
    if duration_ms < 0.001:
        return None
    return Span(
        span_id=f"context_load:{trace_id}",
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        kind=SpanKind.CONTEXT_LOAD,
        name="Context Load",
        start_time=start,
        end_time=end,
        duration_ms=duration_ms,
        loop_index=None,
        attributes={},
        children=(),
        status="ok",
    )


_STEP_EVENT_TYPES = frozenset({
    RuntimeEventType.LLM_THINK_STARTED.value,
    RuntimeEventType.LLM_THINK_COMPLETED.value,
    RuntimeEventType.TOOL_CALL_STARTED.value,
    RuntimeEventType.TOOL_CALL_COMPLETED.value,
    RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
    RuntimeEventType.STEP_COMPLETED.value,
    RuntimeEventType.STEP_FAILED.value,
})


def _group_by_step(events: list[RuntimeEvent]) -> dict[int, list[RuntimeEvent]]:
    groups: dict[int, list[RuntimeEvent]] = {}
    for event in events:
        if event.loop_index is None:
            continue
        if event.event_type not in _STEP_EVENT_TYPES:
            continue
        groups.setdefault(int(event.loop_index), []).append(event)
    return groups


def _build_step_span(
    trace_id: str,
    parent_span_id: str,
    loop_index: int,
    events: list[RuntimeEvent],
) -> Span:
    step_span_id = f"step:{trace_id}:{loop_index}"
    children: list[Span] = []
    step_status = "ok"
    tool_call_ordinal = 0

    think_started_at: float | None = None
    for event in events:
        et = event.event_type
        payload = event.payload or {}

        if et == RuntimeEventType.LLM_THINK_STARTED.value:
            think_started_at = float(event.created_at)

        elif et == RuntimeEventType.LLM_THINK_COMPLETED.value:
            dur = _payload_duration_ms(payload)
            end_time = float(event.created_at)
            if dur is not None and think_started_at is None:
                start_time = end_time - dur / 1000.0
            else:
                start_time = think_started_at or end_time
            if dur is None:
                dur = round((end_time - start_time) * 1000.0, 3)
            children.append(Span(
                span_id=f"think:{trace_id}:{loop_index}",
                trace_id=trace_id,
                parent_span_id=step_span_id,
                kind=SpanKind.THINK,
                name="LLM Think",
                start_time=start_time,
                end_time=end_time,
                duration_ms=dur,
                loop_index=loop_index,
                attributes={
                    k: v for k, v in {
                        "action_type": payload.get("action_type"),
                        "token_usage": payload.get("token_usage"),
                        "tool_calls": payload.get("tool_calls"),
                    }.items() if v is not None
                },
                children=(),
                status="ok",
            ))
            think_started_at = None

        elif et in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        }:
            dur = _payload_duration_ms(payload)
            end_time = float(event.created_at)
            if dur is not None:
                start_time = end_time - dur / 1000.0
            else:
                start_time = end_time
                dur = 0.0

            tool_name = _extract_tool_name(payload)
            is_subagent = et == RuntimeEventType.SUBAGENT_CALL_COMPLETED.value
            kind = SpanKind.SUBAGENT_CALL if is_subagent else SpanKind.TOOL_CALL
            display_name = f"Subagent: {_extract_subagent_id(payload)}" if is_subagent else f"Tool: {tool_name}"

            attrs: dict[str, Any] = {"tool_name": tool_name}
            if is_subagent:
                result_meta = _extract_result_metadata(payload)
                attrs["agent_id"] = result_meta.get("agent_id", "")
                attrs["subagent_session_id"] = result_meta.get("subagent_session_id", "")
                attrs["request_id"] = result_meta.get("request_id")

            is_error = _is_tool_error(payload)
            children.append(Span(
                span_id=f"{'subagent_call' if is_subagent else 'tool_call'}:{trace_id}:{loop_index}:{tool_call_ordinal}",
                trace_id=trace_id,
                parent_span_id=step_span_id,
                kind=kind,
                name=display_name,
                start_time=start_time,
                end_time=end_time,
                duration_ms=dur,
                loop_index=loop_index,
                attributes=attrs,
                children=(),
                status="error" if is_error else "ok",
            ))
            tool_call_ordinal += 1

        elif et == RuntimeEventType.STEP_FAILED.value:
            step_status = "error"

    step_start = min((c.start_time for c in children), default=float(events[0].created_at))
    step_completed = [e for e in events if e.event_type == RuntimeEventType.STEP_COMPLETED.value]
    if step_completed:
        step_end = float(step_completed[-1].created_at)
        step_dur = _payload_duration_ms(step_completed[-1].payload or {})
    else:
        step_end = max((c.end_time for c in children), default=float(events[-1].created_at))
        step_dur = None
    if step_dur is None:
        step_dur = round((step_end - step_start) * 1000.0, 3)

    return Span(
        span_id=step_span_id,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        kind=SpanKind.STEP,
        name=f"Step {loop_index}",
        start_time=step_start,
        end_time=step_end,
        duration_ms=step_dur,
        loop_index=loop_index,
        attributes={},
        children=tuple(children),
        status=step_status,
    )


def _payload_duration_ms(payload: dict[str, Any]) -> float | None:
    raw = payload.get("duration_ms")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _extract_tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "name"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("tool_name", "name"):
            raw = result.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "unknown"


def _extract_subagent_id(payload: dict[str, Any]) -> str:
    result_meta = _extract_result_metadata(payload)
    agent_id = result_meta.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    return _extract_tool_name(payload)


def _extract_result_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if isinstance(result, dict):
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            return metadata
        return result
    return payload


def _is_tool_error(payload: dict[str, Any]) -> bool:
    if payload.get("is_error") is True:
        return True
    result = payload.get("result")
    if isinstance(result, dict) and result.get("is_error") is True:
        return True
    status = str(payload.get("status") or "").lower()
    return status in {"error", "failed"}


def serialize_span(span: Span) -> dict[str, Any]:
    return {
        "span_id": span.span_id,
        "kind": span.kind.value,
        "name": span.name,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ms": round(span.duration_ms, 3),
        "loop_index": span.loop_index,
        "status": span.status,
        "attributes": span.attributes,
        "children": [serialize_span(c) for c in span.children],
    }


def _first_text(values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

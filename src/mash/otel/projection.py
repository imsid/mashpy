"""Project a request's runtime events onto OpenTelemetry spans and log records.

The admin UI reads this projection and the exporter sends it over OTLP, so
both show the same span names, attributes and events. It is plain Python with
no OpenTelemetry dependency.

Each stored event opens a span, closes one, or becomes a log record (see
``RuntimeEventType``). Events already carry their span's name and their
attributes under OpenTelemetry names, so the work here is pairing each start
with its end, deriving ids and parents, and merging the two rows'
``attributes``. A span whose end is not stored yet comes back with
``end_time`` None.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..runtime.events.types import (
    EndStatus,
    RuntimeEvent,
    RuntimeEventType,
    event_attributes,
)
from . import semconv
from .context import parse_traceparent
from .ids import span_id_hex, trace_id_hex

KIND_INTERNAL = "INTERNAL"
KIND_CLIENT = "CLIENT"
STATUS_UNSET = "UNSET"
STATUS_ERROR = "ERROR"
SEVERITY_INFO = ("INFO", 9)
SEVERITY_ERROR = ("ERROR", 17)

# Children of RunSubAgents nest one level deep today; the cap guards a loop.
MAX_CHILD_DEPTH = 3
CAPTURE_CONTENT_ENV = "MASH_OTEL_CAPTURE_CONTENT"
_CONTENT_RESULT_MAX_CHARS = 8_000


def capture_content_enabled() -> bool:
    """Whether ``MASH_OTEL_CAPTURE_CONTENT`` asks for prompts, outputs and tool I/O."""
    return os.environ.get(CAPTURE_CONTENT_ENV, "").strip().lower() in {"1", "true", "yes"}


def default_resource() -> dict[str, Any]:
    return {semconv.SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME") or "mash"}


@dataclass
class Span:
    """One span: a trace-wide id, a key stable within its request, and its times.

    ``start_time``/``end_time`` are unix seconds; ``end_time`` is None while
    the span's closing event is not stored.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    key: str
    name: str
    kind: str
    start_time: float
    end_time: float | None = None
    status: str = STATUS_UNSET
    status_message: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    agent_id: str | None = None

    @property
    def ended(self) -> bool:
        return self.end_time is not None

    def duration_ms(self, now: float | None = None) -> float:
        end = self.end_time if self.end_time is not None else now
        if end is None:
            return 0.0
        return round(max(0.0, end - self.start_time) * 1000.0, 3)

    @property
    def operation(self) -> str | None:
        value = self.attributes.get(semconv.GEN_AI_OPERATION_NAME)
        return str(value) if value else None


@dataclass(frozen=True)
class LogRecord:
    """A named event correlated with a span by trace id and span id."""

    event_name: str
    timestamp: float
    trace_id: str
    span_id: str | None
    severity_text: str
    severity_number: int
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any = None


@dataclass(frozen=True)
class Projection:
    trace_id: str
    root_span_id: str | None
    spans: tuple[Span, ...]
    logs: tuple[LogRecord, ...]
    resource: dict[str, Any]
    # The latest stored event time; an open span's duration runs to here.
    observed_at: float

    def span(self, span_id: str | None) -> Span | None:
        return next((s for s in self.spans if s.span_id == span_id), None)

    def children(self, span_id: str | None) -> list[Span]:
        return [s for s in self.spans if s.parent_span_id == span_id]

    def logs_for(self, span_id: str | None) -> list[LogRecord]:
        return [r for r in self.logs if r.span_id == span_id]

    def subtree(self, span_id: str) -> list[Span]:
        """The span and all its descendants, parent first."""
        result: list[Span] = []
        pending = [span_id]
        while pending:
            current = pending.pop(0)
            span = self.span(current)
            if span is None:
                continue
            result.append(span)
            pending.extend(child.span_id for child in self.children(current))
        return result


class _Builder:
    def __init__(self, *, capture_content: bool) -> None:
        self.capture_content = capture_content
        self.spans: list[Span] = []
        self.logs: list[LogRecord] = []
        self.observed_at = 0.0

    def log(
        self,
        name: str,
        timestamp: float,
        span: Span | None,
        trace_id: str,
        attributes: Mapping[str, Any],
        *,
        severity: tuple[str, int] = SEVERITY_INFO,
        body: Any = None,
    ) -> None:
        self.logs.append(
            LogRecord(
                event_name=name,
                timestamp=timestamp,
                trace_id=trace_id,
                span_id=span.span_id if span is not None else None,
                severity_text=severity[0],
                severity_number=severity[1],
                attributes=dict(attributes),
                body=body,
            )
        )


def project_request(
    events: Iterable[RuntimeEvent],
    *,
    children: Mapping[str, list[RuntimeEvent]] | None = None,
    capture_content: bool | None = None,
    resource: Mapping[str, Any] | None = None,
    parent: tuple[str, str] | None = None,
) -> Projection:
    """Project one request's events, with its RunSubAgents children nested.

    ``children`` maps a child request id to its events. ``parent`` is a
    ``(trace_id, span_id)`` to put the request under, as a workflow step does;
    otherwise a stored ``traceparent`` or the request's own trace id is used.
    """
    builder = _Builder(
        capture_content=capture_content_enabled() if capture_content is None else capture_content
    )
    ordered = _ordered(events)
    trace_hex, parent_span_id = (parent if parent is not None else (None, None))
    root = _project_into(
        builder,
        ordered,
        trace_hex=trace_hex,
        parent_span_id=parent_span_id,
        children=children or {},
        depth=0,
    )
    return _finish(builder, root, resource)


def _finish(builder: _Builder, root: Span | None, resource: Mapping[str, Any] | None) -> Projection:
    spans = sorted(builder.spans, key=lambda s: (s.start_time, s.span_id))
    return Projection(
        trace_id=root.trace_id if root is not None else "",
        root_span_id=root.span_id if root is not None else None,
        spans=tuple(spans),
        logs=tuple(sorted(builder.logs, key=lambda r: r.timestamp)),
        resource=dict(resource) if resource is not None else default_resource(),
        observed_at=builder.observed_at,
    )


def _ordered(events: Iterable[RuntimeEvent]) -> list[RuntimeEvent]:
    return sorted(
        events,
        key=lambda e: (
            e.request_seq if e.request_seq is not None else 0,
            float(e.created_at),
            int(e.event_id),
        ),
    )


def _end_status(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    return status if status in {s.value for s in EndStatus} else EndStatus.OK.value


def _project_into(
    builder: _Builder,
    events: list[RuntimeEvent],
    *,
    trace_hex: str | None,
    parent_span_id: str | None,
    children: Mapping[str, list[RuntimeEvent]],
    depth: int,
) -> Span | None:
    if not events:
        return None
    request_id = next((e.request_id for e in events if e.request_id), None) or "unknown"
    agent_id = events[0].agent_id
    start_event = next(
        (e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value),
        None,
    )
    if trace_hex is None:
        traceparent = parse_traceparent(
            (start_event.payload or {}).get("traceparent") if start_event else None
        )
        if traceparent is not None:
            trace_hex, parent_span_id = traceparent.trace_id, traceparent.span_id
        else:
            trace_hex = trace_id_hex(next((e.trace_id for e in events if e.trace_id), None))
    for event in events:
        builder.observed_at = max(builder.observed_at, float(event.created_at))

    spans: dict[str, Span] = {}

    def open_span(
        key: str,
        name: str,
        start: float,
        parent: Span | None,
        *,
        kind: str = KIND_INTERNAL,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        span = spans.get(key)
        if span is None:
            span = Span(
                trace_id=trace_hex,
                span_id=span_id_hex(request_id, key),
                parent_span_id=parent.span_id if parent is not None else parent_span_id,
                key=key,
                name=name,
                kind=kind,
                start_time=start,
                request_id=request_id,
                agent_id=agent_id,
            )
            spans[key] = span
            builder.spans.append(span)
        if attributes:
            span.attributes.update(attributes)
        return span

    def close_span(span: Span, at: float, payload: Mapping[str, Any]) -> None:
        span.end_time = at
        span.attributes.update(event_attributes(dict(payload)))
        if _end_status(payload) == EndStatus.ERROR.value:
            span.status = STATUS_ERROR
            span.status_message = _clean(payload.get("error"))
        else:
            span.status = STATUS_UNSET
            span.status_message = None

    root_start = float((start_event or events[0]).created_at)
    root = open_span(
        "root",
        f"{semconv.OP_INVOKE_AGENT} {agent_id}",
        root_start,
        None,
        attributes=event_attributes((start_event.payload if start_event else None)),
    )
    context_load: Span | None = None
    current_step: Span | None = None

    for event in events:
        kind = event.event_type
        payload = event.payload or {}
        at = float(event.created_at)
        attributes = event_attributes(payload)
        loop_index = event.loop_index

        if kind == RuntimeEventType.CONTEXT_LOAD_START.value:
            cold = open_span("cold_start", "mash.cold_start", root_start, root)
            cold.end_time = at
            context_load = open_span("context_load", "mash.context_load", at, root)
        elif kind == RuntimeEventType.CONTEXT_LOAD_END.value:
            if context_load is None:
                context_load = open_span("context_load", "mash.context_load", at, root)
            close_span(context_load, at, payload)
        elif kind in _STEP_EVENTS and loop_index is not None:
            step = spans.get(f"step.{loop_index}")
            if step is None:
                step = open_span(
                    f"step.{loop_index}",
                    f"mash.step {loop_index}",
                    at,
                    root,
                    attributes={semconv.MASH_LOOP_INDEX: loop_index},
                )
            current_step = step
            _step_event(builder, event, step, open_span, close_span, children, depth)
            if kind == RuntimeEventType.STEP_END.value:
                close_span(step, at, payload)
                current_step = None
        elif kind == RuntimeEventType.INTERACTION_START.value:
            interaction_type = str(payload.get("type") or "info")
            open_span(
                f"interaction.{payload.get('interaction_id')}",
                f"mash.interaction {interaction_type}",
                at,
                current_step or root,
                attributes=attributes,
            )
        elif kind == RuntimeEventType.INTERACTION_END.value:
            span = spans.get(f"interaction.{payload.get('interaction_id')}")
            if span is not None:
                close_span(span, at, payload)
        elif kind == RuntimeEventType.INVOKE_AGENT_END.value:
            status = _end_status(payload)
            innermost = _innermost_open(spans.values(), root)
            for span in spans.values():
                if span is not root and span.end_time is None:
                    span.end_time = at
            close_span(root, at, payload)
            if status == EndStatus.ERROR.value:
                error_type = str(attributes.get(semconv.ERROR_TYPE) or "error")
                if innermost is not root:
                    innermost.status = STATUS_ERROR
                    innermost.status_message = _clean(payload.get("error"))
                    innermost.attributes[semconv.ERROR_TYPE] = error_type
                exception = {semconv.EXCEPTION_TYPE: error_type}
                message = _clean(payload.get("error"))
                if message:
                    exception[semconv.EXCEPTION_MESSAGE] = message
                builder.log(
                    semconv.EXCEPTION_EVENT,
                    at,
                    innermost,
                    trace_hex,
                    exception,
                    severity=SEVERITY_ERROR,
                )
            current_step = None
        elif kind == RuntimeEventType.REQUEST_RESUMED.value:
            root.end_time = None
            root.status = STATUS_UNSET
            root.status_message = None
            builder.log(kind, at, root, trace_hex, attributes)
        elif kind == RuntimeEventType.REQUEST_MODE_CHANGED.value:
            builder.log(kind, at, root, trace_hex, attributes)
    return root


_STEP_EVENTS = {
    RuntimeEventType.CHAT_START.value,
    RuntimeEventType.CHAT_END.value,
    RuntimeEventType.EXECUTE_TOOL_START.value,
    RuntimeEventType.EXECUTE_TOOL_END.value,
    RuntimeEventType.STEP_END.value,
}


def _step_event(
    builder: _Builder,
    event: RuntimeEvent,
    step: Span,
    open_span: Any,
    close_span: Any,
    children: Mapping[str, list[RuntimeEvent]],
    depth: int,
) -> None:
    kind = event.event_type
    payload = event.payload or {}
    at = float(event.created_at)
    attributes = event_attributes(payload)
    loop_index = event.loop_index

    if kind in {RuntimeEventType.CHAT_START.value, RuntimeEventType.CHAT_END.value}:
        model = attributes.get(semconv.GEN_AI_REQUEST_MODEL) or "unknown"
        start = at
        if kind == RuntimeEventType.CHAT_END.value:
            duration_ms = _number(payload.get("duration_ms"))
            if duration_ms is not None:
                start = at - duration_ms / 1000.0
        chat = open_span(
            f"chat.{loop_index}",
            f"{semconv.OP_CHAT} {model}",
            start,
            step,
            kind=KIND_CLIENT,
            attributes=attributes,
        )
        chat.name = f"{semconv.OP_CHAT} {chat.attributes.get(semconv.GEN_AI_REQUEST_MODEL) or model}"
        if kind == RuntimeEventType.CHAT_END.value:
            close_span(chat, at, payload)
            if builder.capture_content:
                builder.log(
                    semconv.GEN_AI_INFERENCE_DETAILS_EVENT,
                    at,
                    chat,
                    chat.trace_id,
                    {
                        semconv.GEN_AI_OPERATION_NAME: semconv.OP_CHAT,
                        semconv.GEN_AI_OUTPUT_MESSAGES: _output_messages(payload),
                    },
                )
        return

    if kind in {
        RuntimeEventType.EXECUTE_TOOL_START.value,
        RuntimeEventType.EXECUTE_TOOL_END.value,
    }:
        call_id = attributes.get(semconv.GEN_AI_TOOL_CALL_ID) or "unknown"
        tool_name = attributes.get(semconv.GEN_AI_TOOL_NAME) or "unknown"
        start = at
        if kind == RuntimeEventType.EXECUTE_TOOL_END.value:
            duration_ms = _number(payload.get("duration_ms"))
            if duration_ms is not None:
                start = at - duration_ms / 1000.0
        tool = open_span(
            f"tool.{loop_index}.{call_id}",
            f"{semconv.OP_EXECUTE_TOOL} {tool_name}",
            start,
            step,
            attributes=attributes,
        )
        if kind == RuntimeEventType.EXECUTE_TOOL_START.value:
            if builder.capture_content:
                tool.attributes[semconv.GEN_AI_TOOL_CALL_ARGUMENTS] = _json_text(
                    payload.get("arguments") or {}
                )
            for item in payload.get("subagents") or []:
                if not isinstance(item, dict) or depth + 1 > MAX_CHILD_DEPTH:
                    continue
                child_events = children.get(str(item.get("request_id") or ""))
                if child_events:
                    _project_into(
                        builder,
                        _ordered(child_events),
                        trace_hex=tool.trace_id,
                        parent_span_id=tool.span_id,
                        children=children,
                        depth=depth + 1,
                    )
            return
        close_span(tool, at, payload)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if builder.capture_content:
            tool.attributes[semconv.GEN_AI_TOOL_CALL_RESULT] = _json_text(
                result.get("content")
            )[:_CONTENT_RESULT_MAX_CHARS]
        if _end_status(payload) == EndStatus.ERROR.value:
            exception = {
                semconv.EXCEPTION_TYPE: str(attributes.get(semconv.ERROR_TYPE) or "tool_error")
            }
            if builder.capture_content:
                exception[semconv.EXCEPTION_MESSAGE] = _json_text(result.get("content"))[
                    :_CONTENT_RESULT_MAX_CHARS
                ]
            builder.log(
                semconv.EXCEPTION_EVENT,
                at,
                tool,
                tool.trace_id,
                exception,
                severity=SEVERITY_ERROR,
            )


def _innermost_open(spans: Iterable[Span], root: Span) -> Span:
    """The most recently started span still open, else the root.

    ``spans`` is in creation order, and a span is created after its parent,
    so on a tie in start time the later one is the inner one.
    """
    open_spans = [
        (span.start_time, order, span)
        for order, span in enumerate(spans)
        if span is not root and span.end_time is None
    ]
    if not open_spans:
        return root
    return max(open_spans, key=lambda item: (item[0], item[1]))[2]


def _output_messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = payload.get("assistant_text")
    if isinstance(text, str) and text:
        parts.append({"type": "text", "content": text})
    for call in payload.get("tool_calls") or []:
        if isinstance(call, dict):
            parts.append(
                {
                    "type": "tool_call",
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "arguments": call.get("arguments"),
                }
            )
    finish = payload.get("stop_reason")
    message: dict[str, Any] = {"role": "assistant", "parts": parts}
    if finish:
        message["finish_reason"] = str(finish)
    return [message]


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def serialize_span(projection: Projection, span: Span) -> dict[str, Any]:
    """One span and its subtree as JSON, each with its log records."""
    return {
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "trace_id": span.trace_id,
        "name": span.name,
        "kind": span.kind,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ms": span.duration_ms(projection.observed_at),
        "status": span.status,
        "status_message": span.status_message,
        "attributes": dict(span.attributes),
        "logs": [serialize_log(record) for record in projection.logs_for(span.span_id)],
        "children": [
            serialize_span(projection, child) for child in projection.children(span.span_id)
        ],
    }


def serialize_log(record: LogRecord) -> dict[str, Any]:
    return {
        "event_name": record.event_name,
        "timestamp": record.timestamp,
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "severity_text": record.severity_text,
        "severity_number": record.severity_number,
        "attributes": dict(record.attributes),
        "body": record.body,
    }


def serialize_projection(projection: Projection) -> dict[str, Any]:
    root = projection.span(projection.root_span_id)
    return {
        "trace_id": projection.trace_id,
        "resource": dict(projection.resource),
        "semconv_version": semconv.SEMCONV_VERSION,
        "root": serialize_span(projection, root) if root is not None else None,
        "span_count": len(projection.spans),
        "log_count": len(projection.logs),
    }

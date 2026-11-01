"""Deterministic trace analysis over the OpenTelemetry projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...otel import semconv

if TYPE_CHECKING:
    from ...otel.projection import Projection, Span

_RUN_SUBAGENTS = "RunSubAgents"


@dataclass(frozen=True)
class ToolCallStats:
    tool_name: str
    count: int
    total_ms: float
    avg_ms: float
    max_ms: float
    min_ms: float
    error_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "min_ms": round(self.min_ms, 3),
            "error_count": self.error_count,
        }


@dataclass(frozen=True)
class StepBreakdown:
    step_index: int
    think_ms: float
    tool_ms: float
    subagent_ms: float
    overhead_ms: float
    total_ms: float
    tool_calls: list[str]
    token_usage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "think_ms": round(self.think_ms, 3),
            "tool_ms": round(self.tool_ms, 3),
            "subagent_ms": round(self.subagent_ms, 3),
            "overhead_ms": round(self.overhead_ms, 3),
            "total_ms": round(self.total_ms, 3),
            "tool_calls": self.tool_calls,
            "token_usage": self.token_usage,
        }


@dataclass(frozen=True)
class SubagentDetail:
    agent_id: str
    subagent_session_id: str
    request_id: str | None
    duration_ms: float
    child_analysis: "TraceAnalysis | None" = None

    def to_dict(self, *, include_children: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent_id": self.agent_id,
            "subagent_session_id": self.subagent_session_id,
            "request_id": self.request_id,
            "duration_ms": round(self.duration_ms, 3),
        }
        if include_children and self.child_analysis is not None:
            result["child_analysis"] = self.child_analysis.to_digest_dict()
        return result


@dataclass(frozen=True)
class TraceAnalysis:
    trace_id: str
    target_agent_id: str
    session_id: str
    status: str

    total_duration_ms: float
    cold_start_ms: float
    context_load_ms: float
    total_think_ms: float
    total_tool_ms: float
    total_subagent_ms: float
    idle_ms: float

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    step_count: int
    tool_call_count: int
    tool_error_count: int

    tool_stats: list[ToolCallStats]
    step_breakdown: list[StepBreakdown]
    slowest_spans: list[dict[str, Any]]
    subagent_details: list[SubagentDetail]

    def pct(self, value: float) -> float:
        if self.total_duration_ms <= 0:
            return 0.0
        return round(value / self.total_duration_ms * 100.0, 1)

    def to_timing_dict(self) -> dict[str, Any]:
        return {
            "total_duration_ms": round(self.total_duration_ms, 3),
            "cold_start_ms": round(self.cold_start_ms, 3),
            "context_load_ms": round(self.context_load_ms, 3),
            "total_think_ms": round(self.total_think_ms, 3),
            "total_tool_ms": round(self.total_tool_ms, 3),
            "total_subagent_ms": round(self.total_subagent_ms, 3),
            "idle_ms": round(self.idle_ms, 3),
            "pct_think": self.pct(self.total_think_ms),
            "pct_tool": self.pct(self.total_tool_ms),
            "pct_subagent": self.pct(self.total_subagent_ms),
            "pct_cold_start": self.pct(self.cold_start_ms),
        }

    def to_digest_dict(self) -> dict[str, Any]:
        return {
            "timing": self.to_timing_dict(),
            "tool_stats": [s.to_dict() for s in self.tool_stats],
            "step_breakdown": [s.to_dict() for s in self.step_breakdown],
            "slowest_operations": self.slowest_spans[:10],
            "subagent_traces": [
                d.to_dict(include_children=True) for d in self.subagent_details
            ],
            "step_count": self.step_count,
            "tool_call_count": self.tool_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


def analyze_projection(
    projection: "Projection", root_span_id: str | None = None
) -> TraceAnalysis:
    """Timing, tokens and counts for one ``invoke_agent`` span and its own spans.

    A RunSubAgents child's spans belong to the child: they are analysed on
    their own and attached as ``subagent_details``.
    """
    root = projection.span(root_span_id or projection.root_span_id)
    if root is None:
        raise ValueError("projection has no root span")
    now = projection.observed_at
    own = _own_spans(projection, root)

    cold_start_ms = context_load_ms = 0.0
    total_think_ms = total_tool_ms = total_subagent_ms = 0.0
    tool_call_count = tool_error_count = 0
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tool_spans: list["Span"] = []
    subagent_details: list[SubagentDetail] = []

    for span in own:
        duration = span.duration_ms(now)
        if span.name == "mash.cold_start":
            cold_start_ms += duration
        elif span.name == "mash.context_load":
            context_load_ms += duration
        elif span.operation == semconv.OP_CHAT:
            total_think_ms += duration
            usage = semconv.token_usage(span.attributes)
            for key in tokens:
                tokens[key] += usage[key]
        elif span.operation == semconv.OP_EXECUTE_TOOL:
            tool_call_count += 1
            if span.status == "ERROR":
                tool_error_count += 1
            tool_spans.append(span)
            if _tool_name(span) == _RUN_SUBAGENTS:
                total_subagent_ms += duration
                subagent_details.extend(_subagent_details(projection, span))
            else:
                total_tool_ms += duration

    total_duration_ms = root.duration_ms(now)
    idle_ms = max(
        0.0,
        total_duration_ms
        - cold_start_ms
        - context_load_ms
        - total_think_ms
        - total_tool_ms
        - total_subagent_ms,
    )
    steps = [
        child for child in projection.children(root.span_id) if child.name.startswith("mash.step")
    ]
    return TraceAnalysis(
        trace_id=root.trace_id,
        target_agent_id=root.agent_id or "",
        session_id=str(root.attributes.get(semconv.GEN_AI_CONVERSATION_ID) or ""),
        status=root_status(root),
        total_duration_ms=total_duration_ms,
        cold_start_ms=cold_start_ms,
        context_load_ms=context_load_ms,
        total_think_ms=total_think_ms,
        total_tool_ms=total_tool_ms,
        total_subagent_ms=total_subagent_ms,
        idle_ms=idle_ms,
        input_tokens=tokens["input"],
        output_tokens=tokens["output"],
        cache_read_tokens=tokens["cache_read"],
        cache_write_tokens=tokens["cache_write"],
        step_count=len(steps),
        tool_call_count=tool_call_count,
        tool_error_count=tool_error_count,
        tool_stats=_compute_tool_stats(tool_spans, now),
        step_breakdown=[_step_breakdown(projection, step, now) for step in steps],
        slowest_spans=_compute_slowest_spans(own, now),
        subagent_details=subagent_details,
    )


def root_status(root: "Span") -> str:
    """``in_progress``, ``completed``, ``error`` or ``cancelled`` for an agent run."""
    if root.end_time is None:
        return "in_progress"
    if root.status == "ERROR":
        return "error"
    if root.attributes.get(semconv.MASH_REQUEST_STATUS) == "cancelled":
        return "cancelled"
    return "completed"


def _own_spans(projection: "Projection", root: "Span") -> list["Span"]:
    """The root's descendants, stopping at nested ``invoke_agent`` spans."""
    result: list["Span"] = []
    pending = list(projection.children(root.span_id))
    while pending:
        span = pending.pop(0)
        if span.operation == semconv.OP_INVOKE_AGENT:
            continue
        result.append(span)
        pending.extend(projection.children(span.span_id))
    return result


def _tool_name(span: "Span") -> str:
    return str(span.attributes.get(semconv.GEN_AI_TOOL_NAME) or span.name)


def _subagent_details(projection: "Projection", tool_span: "Span") -> list[SubagentDetail]:
    details: list[SubagentDetail] = []
    for child in projection.children(tool_span.span_id):
        if child.operation != semconv.OP_INVOKE_AGENT:
            continue
        details.append(
            SubagentDetail(
                agent_id=child.agent_id or "",
                subagent_session_id=str(
                    child.attributes.get(semconv.GEN_AI_CONVERSATION_ID) or ""
                ),
                request_id=child.request_id,
                duration_ms=child.duration_ms(projection.observed_at),
                child_analysis=analyze_projection(projection, child.span_id),
            )
        )
    return details


def _compute_tool_stats(tool_spans: list["Span"], now: float) -> list[ToolCallStats]:
    by_name: dict[str, list["Span"]] = {}
    for span in tool_spans:
        by_name.setdefault(_tool_name(span), []).append(span)

    stats: list[ToolCallStats] = []
    for name, spans in by_name.items():
        durations = [s.duration_ms(now) for s in spans]
        total = sum(durations)
        stats.append(
            ToolCallStats(
                tool_name=name,
                count=len(spans),
                total_ms=total,
                avg_ms=total / len(spans),
                max_ms=max(durations),
                min_ms=min(durations),
                error_count=sum(1 for s in spans if s.status == "ERROR"),
            )
        )
    stats.sort(key=lambda s: s.total_ms, reverse=True)
    return stats


def _step_breakdown(projection: "Projection", step: "Span", now: float) -> StepBreakdown:
    think_ms = tool_ms = subagent_ms = 0.0
    tool_calls: list[str] = []
    token_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for child in projection.children(step.span_id):
        duration = child.duration_ms(now)
        if child.operation == semconv.OP_CHAT:
            think_ms += duration
            usage = semconv.token_usage(child.attributes)
            for key in token_usage:
                token_usage[key] += usage[key]
        elif child.operation == semconv.OP_EXECUTE_TOOL:
            name = _tool_name(child)
            tool_calls.append(name)
            if name == _RUN_SUBAGENTS:
                subagent_ms += duration
            else:
                tool_ms += duration
    total_ms = step.duration_ms(now)
    return StepBreakdown(
        step_index=int(step.attributes.get(semconv.MASH_LOOP_INDEX) or 0),
        think_ms=think_ms,
        tool_ms=tool_ms,
        subagent_ms=subagent_ms,
        overhead_ms=max(0.0, total_ms - think_ms - tool_ms - subagent_ms),
        total_ms=total_ms,
        tool_calls=tool_calls,
        token_usage=token_usage,
    )


def _compute_slowest_spans(
    spans: list["Span"], now: float, limit: int = 10
) -> list[dict[str, Any]]:
    leaves = [
        s
        for s in spans
        if s.operation in {semconv.OP_CHAT, semconv.OP_EXECUTE_TOOL}
        or s.name in {"mash.cold_start", "mash.context_load"}
    ]
    leaves.sort(key=lambda s: s.duration_ms(now), reverse=True)
    result: list[dict[str, Any]] = []
    for span in leaves[:limit]:
        loop_index = None
        if span.key.startswith(("chat.", "tool.")):
            try:
                loop_index = int(span.key.split(".")[1])
            except (IndexError, ValueError):
                loop_index = None
        result.append(
            {
                "kind": span.operation or span.name,
                "name": span.name,
                "duration_ms": round(span.duration_ms(now), 3),
                "step_index": loop_index,
            }
        )
    return result

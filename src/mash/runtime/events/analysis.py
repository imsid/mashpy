"""Deterministic trace analysis from span trees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spans import Span, SpanKind, TraceSpanTree


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


def analyze_trace(tree: TraceSpanTree) -> TraceAnalysis:
    all_spans = _collect_all_spans(tree.root)

    cold_start_ms = 0.0
    context_load_ms = 0.0
    total_think_ms = 0.0
    total_tool_ms = 0.0
    total_subagent_ms = 0.0
    tool_call_count = 0
    tool_error_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0

    tool_call_spans: list[Span] = []
    subagent_spans: list[Span] = []

    for span in all_spans:
        if span.kind == SpanKind.COLD_START:
            cold_start_ms += span.duration_ms
        elif span.kind == SpanKind.CONTEXT_LOAD:
            context_load_ms += span.duration_ms
        elif span.kind == SpanKind.THINK:
            total_think_ms += span.duration_ms
            usage = span.attributes.get("token_usage")
            if isinstance(usage, dict):
                input_tokens += _safe_int(
                    usage.get("input") or usage.get("input_tokens")
                )
                output_tokens += _safe_int(
                    usage.get("output") or usage.get("output_tokens")
                )
                cache_read_tokens += _safe_int(
                    usage.get("cache_read") or usage.get("cache_read_tokens")
                )
                cache_write_tokens += _safe_int(
                    usage.get("cache_write") or usage.get("cache_write_tokens")
                )
        elif span.kind == SpanKind.TOOL_CALL:
            total_tool_ms += span.duration_ms
            tool_call_count += 1
            if span.status == "error":
                tool_error_count += 1
            tool_call_spans.append(span)
        elif span.kind == SpanKind.SUBAGENT_CALL:
            total_subagent_ms += span.duration_ms
            tool_call_count += 1
            if span.status == "error":
                tool_error_count += 1
            subagent_spans.append(span)

    total_duration_ms = tree.root.duration_ms
    idle_ms = max(
        0.0,
        total_duration_ms
        - cold_start_ms
        - context_load_ms
        - total_think_ms
        - total_tool_ms
        - total_subagent_ms,
    )

    tool_stats = _compute_tool_stats(tool_call_spans + subagent_spans)
    step_breakdown = _compute_step_breakdown(tree.root)
    slowest_spans = _compute_slowest_spans(all_spans)
    subagent_details = _extract_subagent_details(subagent_spans)
    step_count = sum(1 for s in all_spans if s.kind == SpanKind.STEP)

    return TraceAnalysis(
        trace_id=tree.trace_id,
        target_agent_id=tree.target_agent_id,
        session_id=tree.session_id,
        status=tree.root.status,
        total_duration_ms=total_duration_ms,
        cold_start_ms=cold_start_ms,
        context_load_ms=context_load_ms,
        total_think_ms=total_think_ms,
        total_tool_ms=total_tool_ms,
        total_subagent_ms=total_subagent_ms,
        idle_ms=idle_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        step_count=step_count,
        tool_call_count=tool_call_count,
        tool_error_count=tool_error_count,
        tool_stats=tool_stats,
        step_breakdown=step_breakdown,
        slowest_spans=slowest_spans,
        subagent_details=subagent_details,
    )


def _collect_all_spans(span: Span) -> list[Span]:
    result = [span]
    for child in span.children:
        result.extend(_collect_all_spans(child))
    return result


def _compute_tool_stats(tool_spans: list[Span]) -> list[ToolCallStats]:
    by_name: dict[str, list[Span]] = {}
    for span in tool_spans:
        name = span.attributes.get("tool_name", span.name)
        by_name.setdefault(name, []).append(span)

    stats: list[ToolCallStats] = []
    for name, spans in by_name.items():
        durations = [s.duration_ms for s in spans]
        total = sum(durations)
        error_count = sum(1 for s in spans if s.status == "error")
        stats.append(
            ToolCallStats(
                tool_name=name,
                count=len(spans),
                total_ms=total,
                avg_ms=total / len(spans),
                max_ms=max(durations),
                min_ms=min(durations),
                error_count=error_count,
            )
        )
    stats.sort(key=lambda s: s.total_ms, reverse=True)
    return stats


def _compute_step_breakdown(root: Span) -> list[StepBreakdown]:
    steps: list[StepBreakdown] = []
    for child in root.children:
        if child.kind != SpanKind.STEP or child.loop_index is None:
            continue

        think_ms = 0.0
        tool_ms = 0.0
        subagent_ms = 0.0
        tool_calls: list[str] = []
        token_usage: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

        for grandchild in child.children:
            if grandchild.kind == SpanKind.THINK:
                think_ms += grandchild.duration_ms
                usage = grandchild.attributes.get("token_usage")
                if isinstance(usage, dict):
                    token_usage["input"] += _safe_int(
                        usage.get("input") or usage.get("input_tokens")
                    )
                    token_usage["output"] += _safe_int(
                        usage.get("output") or usage.get("output_tokens")
                    )
                    token_usage["cache_read"] += _safe_int(
                        usage.get("cache_read") or usage.get("cache_read_tokens")
                    )
                    token_usage["cache_write"] += _safe_int(
                        usage.get("cache_write") or usage.get("cache_write_tokens")
                    )
            elif grandchild.kind == SpanKind.TOOL_CALL:
                tool_ms += grandchild.duration_ms
                tool_calls.append(
                    grandchild.attributes.get("tool_name", grandchild.name)
                )
            elif grandchild.kind == SpanKind.SUBAGENT_CALL:
                subagent_ms += grandchild.duration_ms
                tool_calls.append(
                    grandchild.attributes.get("tool_name", grandchild.name)
                )

        total_ms = child.duration_ms
        overhead_ms = max(0.0, total_ms - think_ms - tool_ms - subagent_ms)

        steps.append(
            StepBreakdown(
                step_index=child.loop_index,
                think_ms=think_ms,
                tool_ms=tool_ms,
                subagent_ms=subagent_ms,
                overhead_ms=overhead_ms,
                total_ms=total_ms,
                tool_calls=tool_calls,
                token_usage=token_usage,
            )
        )
    return steps


def _compute_slowest_spans(
    all_spans: list[Span], limit: int = 10
) -> list[dict[str, Any]]:
    leaf_spans = [
        s
        for s in all_spans
        if s.kind
        in {
            SpanKind.THINK,
            SpanKind.TOOL_CALL,
            SpanKind.SUBAGENT_CALL,
            SpanKind.COLD_START,
            SpanKind.CONTEXT_LOAD,
        }
    ]
    leaf_spans.sort(key=lambda s: s.duration_ms, reverse=True)
    return [
        {
            "kind": span.kind.value,
            "name": span.name,
            "duration_ms": round(span.duration_ms, 3),
            "step_index": span.loop_index,
        }
        for span in leaf_spans[:limit]
    ]


def _extract_subagent_details(subagent_spans: list[Span]) -> list[SubagentDetail]:
    details: list[SubagentDetail] = []
    for span in subagent_spans:
        attrs = span.attributes
        details.append(
            SubagentDetail(
                agent_id=str(attrs.get("agent_id", "")),
                subagent_session_id=str(attrs.get("subagent_session_id", "")),
                request_id=attrs.get("request_id"),
                duration_ms=span.duration_ms,
            )
        )
    return details


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

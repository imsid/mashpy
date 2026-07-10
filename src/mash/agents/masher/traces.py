"""Deterministic trace loading, analysis, and artifact helpers.

These are the pure/effectful primitives behind Masher's all-code workflows
(``masher-trace-digest`` and ``masher-online-eval-curation``). No model
inference happens anywhere in this module.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...runtime.events import (
    RuntimeEvent,
    RuntimeStore,
    RuntimeTrace,
    SubagentDetail,
    TraceAnalysis,
    analyze_trace,
    build_runtime_trace,
    build_span_tree,
)


async def stitch_subagent_traces(
    store: RuntimeStore,
    analysis: TraceAnalysis,
    *,
    max_depth: int = 3,
    _depth: int = 0,
) -> TraceAnalysis:
    if not analysis.subagent_details or _depth >= max_depth:
        return analysis

    updated_details: list[SubagentDetail] = []
    for detail in analysis.subagent_details:
        if not detail.agent_id or not detail.subagent_session_id:
            updated_details.append(detail)
            continue
        try:
            child_events = await store.list_events(
                app_id=detail.agent_id,
                session_id=detail.subagent_session_id,
                limit=None,
            )
        except Exception:
            updated_details.append(detail)
            continue

        if not child_events:
            updated_details.append(detail)
            continue

        try:
            child_tree = build_span_tree(child_events)
            child_analysis = analyze_trace(child_tree)
            child_analysis = await stitch_subagent_traces(
                store, child_analysis, max_depth=max_depth, _depth=_depth + 1,
            )
            updated_details.append(replace(detail, child_analysis=child_analysis))
        except Exception:
            updated_details.append(detail)

    return replace(analysis, subagent_details=updated_details)


async def list_traces_since(
    store: RuntimeStore,
    *,
    target_agent_id: str,
    since_ts: float,
    limit: int,
) -> list[dict[str, Any]]:
    events = await store.list_events(
        app_id=target_agent_id,
        after_event_id=0,
        limit=None,
    )
    grouped: dict[tuple[str, str], list[RuntimeEvent]] = {}
    for event in events:
        if not event.trace_id or not event.session_id:
            continue
        grouped.setdefault((event.session_id, event.trace_id), []).append(event)

    traces: list[dict[str, Any]] = []
    for (session_id, trace_id), trace_events in grouped.items():
        trace_events.sort(key=lambda item: int(item.event_id))
        latest_event_at = max(float(item.created_at) for item in trace_events)
        if latest_event_at <= since_ts:
            continue
        traces.append(
            {
                "target_agent_id": target_agent_id,
                "session_id": session_id,
                "trace_id": trace_id,
                "started_at": min(float(item.created_at) for item in trace_events),
                "latest_event_at": latest_event_at,
                "latest_event_id": max(int(item.event_id) for item in trace_events),
                "event_count": len(trace_events),
            }
        )
    traces.sort(key=lambda item: (float(item["latest_event_at"]), int(item["latest_event_id"])))
    return traces[: max(1, int(limit))]


async def load_trace_events(
    store: RuntimeStore,
    *,
    target_agent_id: str,
    session_id: str,
    trace_id: str,
) -> list[RuntimeEvent]:
    events = await store.list_events(
        app_id=target_agent_id,
        session_id=session_id,
        trace_id=trace_id,
        limit=None,
    )
    if not events:
        raise RuntimeError(
            f"no events found for target/session/trace: {target_agent_id} / {session_id} / {trace_id}"
        )
    return events


async def load_trace_bundle(
    store: RuntimeStore,
    *,
    target_agent_id: str,
    session_id: str,
    trace_id: str,
) -> RuntimeTrace:
    events = await load_trace_events(
        store,
        target_agent_id=target_agent_id,
        session_id=session_id,
        trace_id=trace_id,
    )
    return build_runtime_trace(events)


def build_trace_digest(bundle: RuntimeTrace, analysis: TraceAnalysis) -> dict[str, Any]:
    total = analysis.total_duration_ms
    pct_think = analysis.pct(analysis.total_think_ms)
    pct_tool = analysis.pct(analysis.total_tool_ms)

    summary = (
        f"Trace {bundle.trace_id}: {total:.0f}ms total, "
        f"{pct_think:.0f}% LLM ({analysis.total_think_ms:.0f}ms), "
        f"{pct_tool:.0f}% tools ({analysis.total_tool_ms:.0f}ms), "
        f"{analysis.step_count} steps, {analysis.tool_call_count} tool calls"
    )

    digest: dict[str, Any] = {
        "schema_version": 2,
        "target_agent_id": bundle.target_agent_id,
        "session_id": bundle.session_id,
        "trace_id": bundle.trace_id,
        "status": analysis.status,
        "summary": summary,
        "timing": analysis.to_timing_dict(),
        "tokens": {
            "input_tokens": analysis.input_tokens,
            "output_tokens": analysis.output_tokens,
            "total_tokens": analysis.input_tokens + analysis.output_tokens,
        },
        "counts": {
            "step_count": analysis.step_count,
            "tool_call_count": analysis.tool_call_count,
            "tool_error_count": analysis.tool_error_count,
            "event_count": len(bundle.events),
        },
        "tool_stats": [s.to_dict() for s in analysis.tool_stats],
        "step_breakdown": [s.to_dict() for s in analysis.step_breakdown],
        "slowest_operations": analysis.slowest_spans[:10],
        "subagent_traces": [
            _subagent_detail_to_digest(d) for d in analysis.subagent_details
        ],
        "notable_events": [
            {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "created_at": event["created_at"],
                "summary": _event_summary(event),
            }
            for event in bundle.failed_events[:5]
        ],
    }
    return digest


def _subagent_detail_to_digest(detail: SubagentDetail) -> dict[str, Any]:
    result: dict[str, Any] = {
        "agent_id": detail.agent_id,
        "duration_ms": round(detail.duration_ms, 3),
    }
    if detail.child_analysis is not None:
        child = detail.child_analysis
        result["timing"] = child.to_timing_dict()
        result["tool_stats"] = [s.to_dict() for s in child.tool_stats]
        result["step_breakdown"] = [s.to_dict() for s in child.step_breakdown]
        result["subagent_traces"] = [
            _subagent_detail_to_digest(d) for d in child.subagent_details
        ]
    return result


def build_online_eval_row(bundle: RuntimeTrace, analysis: TraceAnalysis) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "target_agent_id": bundle.target_agent_id,
        "session_id": bundle.session_id,
        "trace_id": bundle.trace_id,
        "user_message": bundle.user_message,
        "assistant_response": bundle.assistant_response,
        "tools_called": bundle.tools_called,
        "tool_call_count": bundle.tool_call_count,
        "step_count": bundle.step_count,
        "input_tokens": bundle.input_tokens,
        "output_tokens": bundle.output_tokens,
        "timing": analysis.to_timing_dict(),
    }


def _event_summary(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    message = payload.get("error") or payload.get("message") or payload.get("status")
    return str(message or event.get("event_type") or "notable event")


def append_jsonl_unique(path: Path, record: dict[str, Any]) -> bool:
    """Append a record keyed by (target_agent_id, session_id, trace_id).

    Returns False without writing when a record with the same key already
    exists, which also makes the at-least-once replay of an append step
    converge instead of duplicating.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    target_agent_id = str(record.get("target_agent_id") or "").strip()
    session_id = str(record.get("session_id") or "").strip()
    trace_id = str(record.get("trace_id") or "").strip()
    if target_agent_id and session_id and trace_id and _has_record(path, target_agent_id, session_id, trace_id):
        return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
    return True


def _has_record(path: Path, target_agent_id: str, session_id: str, trace_id: str) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if (
                    str(payload.get("target_agent_id") or "").strip() == target_agent_id
                    and str(payload.get("session_id") or "").strip() == session_id
                    and str(payload.get("trace_id") or "").strip() == trace_id
                ):
                    return True
    except OSError:
        return False
    return False


__all__ = [
    "append_jsonl_unique",
    "build_online_eval_row",
    "build_trace_digest",
    "list_traces_since",
    "load_trace_bundle",
    "load_trace_events",
    "stitch_subagent_traces",
]

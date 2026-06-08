"""Masher workflow tools and runtime context."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict

from ...runtime.events import (
    RuntimeTrace,
    RuntimeEvent,
    RuntimeStore,
    TraceAnalysis,
    SubagentDetail,
    build_runtime_trace,
    build_span_tree,
    analyze_trace,
)
from ...tools.base import FunctionTool, ToolResult


@dataclass
class MasherRuntimeContext:
    """Runtime dependencies and artifact paths for Masher workflow tools."""

    runtime_store: RuntimeStore | None = None
    trace_digest_jsonl_path: Path | None = None
    online_eval_jsonl_path: Path | None = None

    def bind_runtime_store(self, runtime_store: RuntimeStore) -> None:
        self.runtime_store = runtime_store

    def configure_artifacts(self, data_root: Path) -> None:
        masher_root = data_root / "masher"
        self.trace_digest_jsonl_path = (masher_root / "trace-digests.jsonl").resolve()
        self.online_eval_jsonl_path = (masher_root / "online-evals.jsonl").resolve()

    def require_runtime_store(self) -> RuntimeStore:
        if self.runtime_store is None:
            raise RuntimeError("Masher runtime store is not bound")
        return self.runtime_store

    def require_trace_digest_jsonl_path(self) -> Path:
        if self.trace_digest_jsonl_path is None:
            raise RuntimeError("Masher trace digest artifact path is not configured")
        return self.trace_digest_jsonl_path

    def require_online_eval_jsonl_path(self) -> Path:
        if self.online_eval_jsonl_path is None:
            raise RuntimeError("Masher online eval artifact path is not configured")
        return self.online_eval_jsonl_path


class TraceDigestWorkflowTool(FunctionTool):
    """Run Masher's workflow-only trace digest contract."""

    def __init__(
        self,
        *,
        context: MasherRuntimeContext,
    ) -> None:
        self._context = context
        FunctionTool.__init__(
            self,
            name="run_trace_digest_workflow",
            description=(
                "Execute Masher's workflow input. Supports trace mode for one trace "
                "and incremental mode for all traces after a checkpoint."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_input": {
                        "type": "object",
                        "description": "The workflow_input object from the task request.",
                    },
                    "task_state": {
                        "type": "object",
                        "description": "The task_state checkpoint object from the task request.",
                    },
                },
                "required": ["workflow_input", "task_state"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        workflow_input = args.get("workflow_input")
        task_state = args.get("task_state")
        if not isinstance(workflow_input, dict):
            return ToolResult.error("workflow_input must be an object")
        if not isinstance(task_state, dict):
            return ToolResult.error("task_state must be an object")
        mode = str(workflow_input.get("mode") or "").strip().lower()
        if mode == "trace":
            return await self._run_trace_mode(workflow_input)
        if mode == "incremental":
            return await self._run_incremental_mode(workflow_input, task_state)
        return ToolResult.error("workflow_input.mode must be 'trace' or 'incremental'")

    async def _run_trace_mode(self, workflow_input: dict[str, Any]) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        session_id = _required_text(workflow_input, "session_id")
        trace_id = _required_text(workflow_input, "trace_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        if isinstance(session_id, ToolResult):
            return session_id
        if isinstance(trace_id, ToolResult):
            return trace_id
        store = self._context.require_runtime_store()
        try:
            events = await _load_trace_events(
                store,
                target_agent_id=target_agent_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            bundle = build_runtime_trace(events)
            tree = build_span_tree(events)
            analysis = analyze_trace(tree)
            analysis = await _stitch_subagent_traces(store, analysis)
            digest = _build_trace_digest(bundle, analysis)
        except RuntimeError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(json.dumps(digest, ensure_ascii=True), **digest)

    async def _run_incremental_mode(
        self,
        workflow_input: dict[str, Any],
        task_state: dict[str, Any],
    ) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        checkpoints = task_state.get("checkpoints")
        checkpoint = _checkpoint_for_target(checkpoints, target_agent_id)
        try:
            last_run_ts = float(checkpoint.get("last_run_ts") or 0.0)
        except (AttributeError, TypeError, ValueError):
            last_run_ts = 0.0

        store = self._context.require_runtime_store()
        try:
            limit = max(1, int(workflow_input.get("limit") or 100))
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.limit must be an integer")
        traces = await _list_traces_since(
            store,
            target_agent_id=target_agent_id,
            since_ts=last_run_ts,
            limit=limit,
        )
        digests: list[dict[str, Any]] = []
        for trace in traces:
            session_id = str(trace.get("session_id") or "").strip()
            trace_id = str(trace.get("trace_id") or "").strip()
            if not session_id or not trace_id:
                continue
            try:
                events = await _load_trace_events(
                    store,
                    target_agent_id=target_agent_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
                bundle = build_runtime_trace(events)
                tree = build_span_tree(events)
                analysis = analyze_trace(tree)
                analysis = await _stitch_subagent_traces(store, analysis)
                digests.append(_build_trace_digest(bundle, analysis))
            except RuntimeError:
                continue

        append_results = [
            _append_digest_jsonl(
                self._context.require_trace_digest_jsonl_path(),
                digest,
            )
            for digest in digests
        ]
        previous_checkpoints = dict(checkpoints) if isinstance(checkpoints, dict) else {}
        next_ts = max(
            [last_run_ts]
            + [float(trace.get("latest_event_at") or 0.0) for trace in traces]
        )
        previous_checkpoints[target_agent_id] = {
            "last_run_ts": next_ts,
            "last_trace_ids": [
                str(trace.get("trace_id") or "")
                for trace in traces
                if str(trace.get("trace_id") or "").strip()
            ],
        }
        next_state = {
            "schema_version": 2,
            "checkpoints": previous_checkpoints,
            "processed_trace_count": len(digests),
            "artifact_path": str(self._context.require_trace_digest_jsonl_path()),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(next_state, ensure_ascii=True), **next_state)


class OnlineEvalCurationWorkflowTool(FunctionTool):
    """Run Masher's workflow-only online eval curation contract."""

    def __init__(
        self,
        *,
        context: MasherRuntimeContext,
    ) -> None:
        self._context = context
        FunctionTool.__init__(
            self,
            name="run_online_eval_curation_workflow",
            description=(
                "Execute Masher's online eval curation workflow input. Supports "
                "trace mode for one trace and incremental mode for all traces "
                "after a checkpoint."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_input": {
                        "type": "object",
                        "description": "The workflow_input object from the task request.",
                    },
                    "task_state": {
                        "type": "object",
                        "description": "The task_state checkpoint object from the task request.",
                    },
                },
                "required": ["workflow_input", "task_state"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        workflow_input = args.get("workflow_input")
        task_state = args.get("task_state")
        if not isinstance(workflow_input, dict):
            return ToolResult.error("workflow_input must be an object")
        if not isinstance(task_state, dict):
            return ToolResult.error("task_state must be an object")
        mode = str(workflow_input.get("mode") or "").strip().lower()
        if mode == "trace":
            return await self._run_trace_mode(workflow_input)
        if mode == "incremental":
            return await self._run_incremental_mode(workflow_input, task_state)
        return ToolResult.error("workflow_input.mode must be 'trace' or 'incremental'")

    async def _run_trace_mode(self, workflow_input: dict[str, Any]) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        session_id = _required_text(workflow_input, "session_id")
        trace_id = _required_text(workflow_input, "trace_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        if isinstance(session_id, ToolResult):
            return session_id
        if isinstance(trace_id, ToolResult):
            return trace_id
        store = self._context.require_runtime_store()
        try:
            events = await _load_trace_events(
                store,
                target_agent_id=target_agent_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            bundle = build_runtime_trace(events)
            tree = build_span_tree(events)
            analysis = analyze_trace(tree)
        except RuntimeError as exc:
            return ToolResult.error(str(exc))
        row = _build_online_eval_row(bundle, analysis)
        appended = _append_jsonl_unique(
            self._context.require_online_eval_jsonl_path(),
            row,
        )
        payload = {
            "schema_version": 2,
            "status": "ok",
            "artifact_path": str(self._context.require_online_eval_jsonl_path()),
            "appended": appended,
            "record": row,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True), **payload)

    async def _run_incremental_mode(
        self,
        workflow_input: dict[str, Any],
        task_state: dict[str, Any],
    ) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        checkpoints = task_state.get("checkpoints")
        checkpoint = _checkpoint_for_target(checkpoints, target_agent_id)
        try:
            last_run_ts = float(checkpoint.get("last_run_ts") or 0.0)
        except (AttributeError, TypeError, ValueError):
            last_run_ts = 0.0
        try:
            limit = max(1, int(workflow_input.get("limit") or 100))
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.limit must be an integer")

        store = self._context.require_runtime_store()
        traces = await _list_traces_since(
            store,
            target_agent_id=target_agent_id,
            since_ts=last_run_ts,
            limit=limit,
        )
        rows: list[dict[str, Any]] = []
        for trace in traces:
            session_id = str(trace.get("session_id") or "").strip()
            trace_id = str(trace.get("trace_id") or "").strip()
            if not session_id or not trace_id:
                continue
            try:
                events = await _load_trace_events(
                    store,
                    target_agent_id=target_agent_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
                bundle = build_runtime_trace(events)
                tree = build_span_tree(events)
                analysis = analyze_trace(tree)
            except RuntimeError:
                continue
            rows.append(_build_online_eval_row(bundle, analysis))

        append_results = [
            _append_jsonl_unique(
                self._context.require_online_eval_jsonl_path(),
                row,
            )
            for row in rows
        ]
        previous_checkpoints = dict(checkpoints) if isinstance(checkpoints, dict) else {}
        next_ts = max(
            [last_run_ts]
            + [float(trace.get("latest_event_at") or 0.0) for trace in traces]
        )
        previous_checkpoints[target_agent_id] = {
            "last_run_ts": next_ts,
            "last_trace_ids": [
                str(trace.get("trace_id") or "")
                for trace in traces
                if str(trace.get("trace_id") or "").strip()
            ],
        }
        next_state = {
            "schema_version": 2,
            "checkpoints": previous_checkpoints,
            "processed_trace_count": len(rows),
            "artifact_path": str(self._context.require_online_eval_jsonl_path()),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(next_state, ensure_ascii=True), **next_state)


async def _stitch_subagent_traces(
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
            child_analysis = await _stitch_subagent_traces(
                store, child_analysis, max_depth=max_depth, _depth=_depth + 1,
            )
            updated_details.append(replace(detail, child_analysis=child_analysis))
        except Exception:
            updated_details.append(detail)

    return replace(analysis, subagent_details=updated_details)


async def _list_traces_since(
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


async def _load_trace_events(
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


async def _load_trace_bundle(
    store: RuntimeStore,
    *,
    target_agent_id: str,
    session_id: str,
    trace_id: str,
) -> RuntimeTrace:
    events = await _load_trace_events(
        store,
        target_agent_id=target_agent_id,
        session_id=session_id,
        trace_id=trace_id,
    )
    return build_runtime_trace(events)


def _build_trace_digest(bundle: RuntimeTrace, analysis: TraceAnalysis) -> dict[str, Any]:
    total = analysis.total_duration_ms
    pct_think = analysis._pct(analysis.total_think_ms)
    pct_tool = analysis._pct(analysis.total_tool_ms)

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


def _build_online_eval_row(bundle: RuntimeTrace, analysis: TraceAnalysis) -> dict[str, Any]:
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


def _append_digest_jsonl(path: Path, digest: dict[str, Any]) -> bool:
    return _append_jsonl_unique(path, digest)


def _append_jsonl_unique(path: Path, record: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_agent_id = str(record.get("target_agent_id") or "").strip()
    session_id = str(record.get("session_id") or "").strip()
    trace_id = str(record.get("trace_id") or "").strip()
    if target_agent_id and session_id and trace_id and _has_digest(path, target_agent_id, session_id, trace_id):
        return False
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
    return True


def _has_digest(path: Path, target_agent_id: str, session_id: str, trace_id: str) -> bool:
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


def _checkpoint_for_target(value: Any, target_agent_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    checkpoint = value.get(target_agent_id)
    if isinstance(checkpoint, dict):
        return checkpoint
    return {}


def _required_text(
    payload: dict[str, Any],
    key: str,
    *,
    default: str | None = None,
) -> str | ToolResult:
    raw = payload.get(key)
    if raw is None and default is not None:
        return default
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ToolResult.error(f"workflow_input.{key} is required")


__all__ = [
    "MasherRuntimeContext",
    "OnlineEvalCurationWorkflowTool",
    "TraceDigestWorkflowTool",
    "_build_online_eval_row",
    "_build_trace_digest",
    "_load_trace_bundle",
    "_load_trace_events",
    "_stitch_subagent_traces",
]

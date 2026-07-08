"""Masher workflow tools and runtime context."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from ...evals.service import EvalService


@dataclass
class MasherRuntimeContext:
    """Runtime dependencies and artifact paths for Masher workflow tools."""

    runtime_store: RuntimeStore | None = None
    trace_digest_jsonl_path: Path | None = None
    online_eval_jsonl_path: Path | None = None
    eval_service: "EvalService | None" = None

    def bind_runtime_store(self, runtime_store: RuntimeStore) -> None:
        self.runtime_store = runtime_store

    def bind_eval_service(self, eval_service: "EvalService") -> None:
        self.eval_service = eval_service

    def configure_artifacts(self, data_root: Path) -> None:
        masher_root = data_root / "masher"
        self.trace_digest_jsonl_path = (masher_root / "trace-digests.jsonl").resolve()
        self.online_eval_jsonl_path = (masher_root / "online-evals.jsonl").resolve()

    def require_runtime_store(self) -> RuntimeStore:
        if self.runtime_store is None:
            raise RuntimeError("Masher runtime store is not bound")
        return self.runtime_store

    def require_eval_service(self) -> "EvalService":
        if self.eval_service is None:
            raise RuntimeError("Masher eval service is not bound")
        return self.eval_service

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
                "and batch mode for all of a target's traces (optionally since a "
                "caller-supplied since_ts). Each run is a clean slate — no checkpoints."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_input": {
                        "type": "object",
                        "description": "The workflow_input object from the step request.",
                    },
                },
                "required": ["workflow_input"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        workflow_input = args.get("workflow_input")
        if not isinstance(workflow_input, dict):
            return ToolResult.error("workflow_input must be an object")
        mode = str(workflow_input.get("mode") or "").strip().lower()
        if mode == "trace":
            return await self._run_trace_mode(workflow_input)
        if mode == "batch":
            return await self._run_batch_mode(workflow_input)
        return ToolResult.error("workflow_input.mode must be 'trace' or 'batch'")

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

    async def _run_batch_mode(self, workflow_input: dict[str, Any]) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id

        store = self._context.require_runtime_store()
        try:
            limit = max(1, int(workflow_input.get("limit") or 100))
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.limit must be an integer")
        # Optional explicit window; defaults to all of the target's traces. There
        # is no persisted checkpoint — each run stands alone.
        try:
            since_ts = float(workflow_input.get("since_ts") or 0.0)
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.since_ts must be a number")
        traces = await _list_traces_since(
            store,
            target_agent_id=target_agent_id,
            since_ts=since_ts,
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
        result = {
            "schema_version": 3,
            "processed_trace_count": len(digests),
            "artifact_path": str(self._context.require_trace_digest_jsonl_path()),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(result, ensure_ascii=True), **result)


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
                "trace mode for one trace and batch mode for all of a target's "
                "traces (optionally since a caller-supplied since_ts). Each run is "
                "a clean slate — no checkpoints."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_input": {
                        "type": "object",
                        "description": "The workflow_input object from the step request.",
                    },
                },
                "required": ["workflow_input"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        workflow_input = args.get("workflow_input")
        if not isinstance(workflow_input, dict):
            return ToolResult.error("workflow_input must be an object")
        mode = str(workflow_input.get("mode") or "").strip().lower()
        if mode == "trace":
            return await self._run_trace_mode(workflow_input)
        if mode == "batch":
            return await self._run_batch_mode(workflow_input)
        return ToolResult.error("workflow_input.mode must be 'trace' or 'batch'")

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

    async def _run_batch_mode(self, workflow_input: dict[str, Any]) -> ToolResult:
        target_agent_id = _required_text(workflow_input, "target_agent_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        try:
            limit = max(1, int(workflow_input.get("limit") or 100))
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.limit must be an integer")
        # Optional explicit window; defaults to all of the target's traces. No
        # persisted checkpoint — each run stands alone.
        try:
            since_ts = float(workflow_input.get("since_ts") or 0.0)
        except (TypeError, ValueError):
            return ToolResult.error("workflow_input.since_ts must be a number")

        store = self._context.require_runtime_store()
        traces = await _list_traces_since(
            store,
            target_agent_id=target_agent_id,
            since_ts=since_ts,
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
        result = {
            "schema_version": 3,
            "processed_trace_count": len(rows),
            "artifact_path": str(self._context.require_online_eval_jsonl_path()),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(result, ensure_ascii=True), **result)


class GenSyntheticEvalsWorkflowTool(FunctionTool):
    """Persist a Masher-generated synthetic eval dataset and rubric.

    Masher (the LLM) does the generation; this tool only validates the model's
    output and hands it to the eval service. It never calls an LLM itself.
    """

    def __init__(
        self,
        *,
        context: MasherRuntimeContext,
    ) -> None:
        self._context = context
        FunctionTool.__init__(
            self,
            name="run_gen_synthetic_evals_workflow",
            description=(
                "Persist a generated synthetic eval as a dataset and scoring "
                "rubric for a host. Supply the dataset rows and rubric you "
                "generated from the host's declared capabilities."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host_id": {
                        "type": "string",
                        "description": "The host the eval covers.",
                    },
                    "user_guidance": {
                        "type": "string",
                        "description": "Guidance from workflow_input, verbatim.",
                    },
                    "row_count": {
                        "type": "integer",
                        "description": (
                            "Requested dataset size from workflow_input.row_count "
                            "(default 20, max 100). dataset_rows must contain "
                            "exactly this many rows."
                        ),
                    },
                    "dataset_rows": {
                        "type": "array",
                        "description": (
                            "Exactly row_count generated test-case rows. Each row: "
                            "input, scenario_description, sampling_category, "
                            "expected_behavior, target_agents."
                        ),
                        "items": {"type": "object"},
                    },
                    "rubric": {
                        "type": "object",
                        "description": (
                            "Scoring rubric with global_scoring_prompt and criteria "
                            "(weights summing to 1.0)."
                        ),
                    },
                },
                "required": ["host_id", "dataset_rows", "rubric"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        host_id = _required_text(args, "host_id")
        if isinstance(host_id, ToolResult):
            return host_id

        row_count = _normalize_row_count(args.get("row_count"))
        if isinstance(row_count, ToolResult):
            return row_count

        rows = _normalize_dataset_rows(args.get("dataset_rows"))
        if isinstance(rows, ToolResult):
            return rows
        if len(rows) != row_count:
            return ToolResult.error(
                f"dataset_rows must contain exactly {row_count} rows (got {len(rows)}); "
                "generate the missing rows and call the tool again"
            )

        rubric = _normalize_rubric(args.get("rubric"))
        if isinstance(rubric, ToolResult):
            return rubric

        user_guidance = args.get("user_guidance")
        user_guidance = user_guidance.strip() if isinstance(user_guidance, str) else ""

        service = self._context.require_eval_service()
        eval_ = await service.persist_eval(
            host_id=host_id,
            user_guidance=user_guidance,
            dataset_rows=rows,
            rubric=rubric,
        )
        payload = {
            "eval_id": eval_.eval_id,
            "host_id": eval_.host_id,
            "dataset_id": eval_.dataset_id,
            "rubric_id": eval_.rubric_id,
            "row_count": len(rows),
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True), **payload)


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


_SAMPLING_CATEGORIES = frozenset(
    {
        "random",
        "multi_tool",
        "multi_agent",
        "high_tokens",
        "long_running",
        "short_running",
    }
)
_MAX_DATASET_ROWS = 100
_DEFAULT_DATASET_ROWS = 20
_ROW_TEXT_FIELDS = ("input", "scenario_description", "expected_behavior")
_WEIGHT_SUM_TOLERANCE = 1e-6


def _normalize_row_count(value: Any) -> int | ToolResult:
    """Requested dataset size; defaults when omitted so it is always enforced."""
    if value is None:
        return _DEFAULT_DATASET_ROWS
    try:
        row_count = int(value)
    except (TypeError, ValueError):
        return ToolResult.error("row_count must be an integer")
    if not 1 <= row_count <= _MAX_DATASET_ROWS:
        return ToolResult.error(
            f"row_count must be between 1 and {_MAX_DATASET_ROWS} (got {row_count})"
        )
    return row_count


def _normalize_dataset_rows(value: Any) -> list[dict[str, Any]] | ToolResult:
    if not isinstance(value, list) or not value:
        return ToolResult.error("dataset_rows must be a non-empty array")
    if len(value) > _MAX_DATASET_ROWS:
        return ToolResult.error(
            f"dataset_rows must contain at most {_MAX_DATASET_ROWS} rows"
        )
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            return ToolResult.error(f"dataset_rows[{index}] must be an object")
        row: dict[str, Any] = {}
        for field in _ROW_TEXT_FIELDS:
            text = raw.get(field)
            if not isinstance(text, str) or not text.strip():
                return ToolResult.error(
                    f"dataset_rows[{index}].{field} is required"
                )
            row[field] = text.strip()
        category = raw.get("sampling_category")
        if not isinstance(category, str) or category.strip() not in _SAMPLING_CATEGORIES:
            return ToolResult.error(
                f"dataset_rows[{index}].sampling_category must be one of "
                f"{sorted(_SAMPLING_CATEGORIES)}"
            )
        row["sampling_category"] = category.strip()
        targets = raw.get("target_agents") or []
        if not isinstance(targets, list) or not all(
            isinstance(item, str) for item in targets
        ):
            return ToolResult.error(
                f"dataset_rows[{index}].target_agents must be an array of strings"
            )
        row["target_agents"] = [item.strip() for item in targets if item.strip()]
        rows.append(row)
    return rows


def _normalize_rubric(value: Any) -> dict[str, Any] | ToolResult:
    if not isinstance(value, dict):
        return ToolResult.error("rubric must be an object")
    raw_criteria = value.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        return ToolResult.error("rubric.criteria must be a non-empty array")
    criteria: list[dict[str, Any]] = []
    weight_sum = 0.0
    for index, raw in enumerate(raw_criteria):
        if not isinstance(raw, dict):
            return ToolResult.error(f"rubric.criteria[{index}] must be an object")
        criterion: dict[str, Any] = {}
        for field in ("name", "description", "scoring_prompt"):
            text = raw.get(field)
            if not isinstance(text, str) or not text.strip():
                return ToolResult.error(f"rubric.criteria[{index}].{field} is required")
            criterion[field] = text.strip()
        try:
            weight = float(raw.get("weight", ""))
        except (TypeError, ValueError):
            return ToolResult.error(
                f"rubric.criteria[{index}].weight must be a number"
            )
        criterion["weight"] = weight
        weight_sum += weight
        criterion["scale_min"] = int(raw.get("scale_min", 1))
        criterion["scale_max"] = int(raw.get("scale_max", 5))
        criteria.append(criterion)
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        return ToolResult.error(
            f"rubric.criteria weights must sum to 1.0 (got {weight_sum})"
        )
    global_prompt = value.get("global_scoring_prompt")
    return {
        "global_scoring_prompt": (
            global_prompt.strip() if isinstance(global_prompt, str) else ""
        ),
        "criteria": criteria,
    }


__all__ = [
    "GenSyntheticEvalsWorkflowTool",
    "MasherRuntimeContext",
    "OnlineEvalCurationWorkflowTool",
    "TraceDigestWorkflowTool",
    "_build_online_eval_row",
    "_build_trace_digest",
    "_load_trace_bundle",
    "_load_trace_events",
    "_stitch_subagent_traces",
]

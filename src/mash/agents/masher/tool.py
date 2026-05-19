"""Masher workflow tools and runtime context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ...runtime.events import (
    RuntimeTrace,
    RuntimeEvent,
    RuntimeStore,
    build_runtime_trace,
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
        target_agent_id = _required_text(
            workflow_input,
            "target_agent_id",
        )
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
            bundle = await _load_trace_bundle(
                store,
                target_agent_id=target_agent_id,
                session_id=session_id,
                trace_id=trace_id,
            )
            digest = _build_trace_digest(bundle)
        except RuntimeError as exc:
            return ToolResult.error(str(exc))
        return ToolResult.success(json.dumps(digest, ensure_ascii=True), **digest)

    async def _run_incremental_mode(
        self,
        workflow_input: dict[str, Any],
        task_state: dict[str, Any],
    ) -> ToolResult:
        target_agent_id = _required_text(
            workflow_input,
            "target_agent_id",
        )
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
                bundle = await _load_trace_bundle(
                    store,
                    target_agent_id=target_agent_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
                digests.append(_build_trace_digest(bundle))
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
            + [
                float(trace.get("latest_event_at") or 0.0)
                for trace in traces
            ]
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
            "schema_version": 1,
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
        target_agent_id = _required_text(
            workflow_input,
            "target_agent_id",
        )
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
            bundle = await _load_trace_bundle(
                store,
                target_agent_id=target_agent_id,
                session_id=session_id,
                trace_id=trace_id,
            )
        except RuntimeError as exc:
            return ToolResult.error(str(exc))
        row = _build_online_eval_row(bundle)
        appended = _append_jsonl_unique(
            self._context.require_online_eval_jsonl_path(),
            row,
        )
        payload = {
            "schema_version": 1,
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
        target_agent_id = _required_text(
            workflow_input,
            "target_agent_id",
        )
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
                bundle = await _load_trace_bundle(
                    store,
                    target_agent_id=target_agent_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
            except RuntimeError:
                continue
            rows.append(_build_online_eval_row(bundle))

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
            "schema_version": 1,
            "checkpoints": previous_checkpoints,
            "processed_trace_count": len(rows),
            "artifact_path": str(self._context.require_online_eval_jsonl_path()),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(next_state, ensure_ascii=True), **next_state)


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


async def _load_trace_bundle(
    store: RuntimeStore,
    *,
    target_agent_id: str,
    session_id: str,
    trace_id: str,
) -> RuntimeTrace:
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
    return build_runtime_trace(events)


def _build_trace_digest(bundle: RuntimeTrace) -> dict[str, Any]:
    status = "failed" if bundle.failed_events else "ok"
    summary = (
        f"Trace {bundle.trace_id} for {bundle.target_agent_id} had "
        f"{len(bundle.events)} events, {bundle.tool_call_count} tool events, "
        f"and {len(bundle.failed_events)} failure/error events."
    )
    return {
        "schema_version": 1,
        "target_agent_id": bundle.target_agent_id,
        "session_id": bundle.session_id,
        "trace_id": bundle.trace_id,
        "status": status,
        "summary": summary,
        "metrics": {
            "event_count": len(bundle.events),
            "duration_ms": bundle.duration_ms,
            "tool_call_count": bundle.tool_call_count,
            "tool_error_count": bundle.tool_error_count,
            "step_count": bundle.step_count,
            "input_tokens": bundle.input_tokens,
            "output_tokens": bundle.output_tokens,
        },
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


def _build_online_eval_row(bundle: RuntimeTrace) -> dict[str, Any]:
    return {
        "schema_version": 1,
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
    "_load_trace_bundle",
]

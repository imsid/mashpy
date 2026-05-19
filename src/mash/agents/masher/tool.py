"""Masher tools for trace-event retrieval and JSONL writes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ...memory.store import MemoryStore
from ...runtime.events import PostgresRuntimeStore, RuntimeEvent, RuntimeStore
from ...tools.base import FunctionTool, ToolResult


def _resolve_runtime_database_url(explicit_value: str | None = None) -> str:
    value = str(explicit_value or os.getenv("MASH_RUNTIME_DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError(
            "MASH_RUNTIME_DATABASE_URL is required for Masher trace event inspection"
        )
    return value


def _serialize_runtime_event(event: RuntimeEvent) -> dict[str, Any]:
    return {
        "event_id": int(event.event_id),
        "request_id": event.request_id,
        "request_seq": event.request_seq,
        "trace_id": event.trace_id,
        "app_id": event.app_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "event_type": event.event_type,
        "loop_index": event.loop_index,
        "step_key": event.step_key,
        "payload": dict(event.payload or {}),
        "created_at": float(event.created_at),
    }


@dataclass(frozen=True)
class TraceBundle:
    target_agent_id: str
    session_id: str
    trace_id: str
    events: list[dict[str, Any]]
    started_at: float
    latest_event_at: float
    duration_ms: float
    user_message: str
    assistant_response: str
    tools_called: list[str]
    tool_call_count: int
    tool_error_count: int
    step_count: int
    input_tokens: int
    output_tokens: int
    failed_events: list[dict[str, Any]]


class _RuntimeStoreBackedTool:
    def __init__(
        self,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
    ) -> None:
        self._runtime_store = runtime_store
        self._runtime_database_url = runtime_database_url

    async def _store(self) -> RuntimeStore:
        if self._runtime_store is None:
            self._runtime_store = PostgresRuntimeStore(
                _resolve_runtime_database_url(self._runtime_database_url)
            )
        return self._runtime_store


async def _resolve_session_id(
    session_store: MemoryStore,
    *,
    app_id: str,
    args: Dict[str, Any],
) -> tuple[str, ToolResult | None]:
    raw_session_id = args.get("session_id")
    if raw_session_id is None:
        latest_session = await session_store.get_latest_session(app_id=app_id)
        if latest_session is None:
            return "", ToolResult.error("no sessions found for this app")
        return str(latest_session["session_id"]), None
    if isinstance(raw_session_id, str) and raw_session_id.strip():
        return raw_session_id.strip(), None
    return "", ToolResult.error("session_id must be a non-empty string if provided")


class GetTraceEventsTool(_RuntimeStoreBackedTool, FunctionTool):
    """Return canonical trace events for a session or a specific trace."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        app_id: str,
        source_label: str = "runtime_event_log",
    ) -> None:
        self._app_id = app_id
        self._source_label = source_label
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
        FunctionTool.__init__(
            self,
            name="get_trace_events",
            description=(
                "Fetch canonical runtime events for a session, or for a session plus "
                "trace_id pair."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Resolved session id to fetch events for.",
                    },
                    "trace_id": {
                        "type": "string",
                        "description": (
                            "Optional resolved trace id. If omitted, returns all "
                            "trace events for the session."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default: 2000).",
                        "default": 2000,
                    },
                    "target_agent_id": {
                        "type": "string",
                        "description": "Optional target agent/app id. Defaults to the configured app id.",
                    },
                },
                "required": ["session_id"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        raw_session_id = args.get("session_id")
        raw_trace_id = args.get("trace_id")
        raw_limit = args.get("limit", 2000)
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            return ToolResult.error(
                "session_id is required and must be a non-empty string"
            )
        if raw_trace_id is not None and not isinstance(raw_trace_id, str):
            return ToolResult.error("trace_id must be a string if provided")
        try:
            limit = max(1, int(raw_limit))
        except (TypeError, ValueError):
            return ToolResult.error("limit must be an integer")

        session_id = raw_session_id.strip()
        trace_id = raw_trace_id.strip() if isinstance(raw_trace_id, str) else None
        target_agent_id = _target_agent_id(args, self._app_id)
        store = await self._store()
        events = [
            _serialize_runtime_event(item)
            for item in await store.list_events(
                app_id=target_agent_id,
                session_id=session_id,
                trace_id=trace_id,
                limit=limit,
            )
        ]
        if not events:
            if trace_id is None:
                return ToolResult.error(f"no events found for session: {session_id}")
            return ToolResult.error(
                f"no events found for session/trace: {session_id} / {trace_id}"
            )

        payload = {
            "source": self._source_label,
            "target_agent_id": target_agent_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "limit": limit,
            "events": events,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2), **payload)


class ListTracesSinceTool(_RuntimeStoreBackedTool, FunctionTool):
    """List trace summaries newer than a checkpoint timestamp."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        app_id: str,
    ) -> None:
        self._app_id = app_id
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
        FunctionTool.__init__(
            self,
            name="list_traces_since",
            description="List trace summaries whose latest event timestamp is after since_ts.",
            parameters={
                "type": "object",
                "properties": {
                    "target_agent_id": {
                        "type": "string",
                        "description": "Target agent/app id. Defaults to the configured app id.",
                    },
                    "since_ts": {
                        "type": "number",
                        "description": "Checkpoint timestamp. Returns traces with latest_event_at greater than this value.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum traces to return (default: 100).",
                        "default": 100,
                    },
                },
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        try:
            since_ts = float(args.get("since_ts") or 0.0)
        except (TypeError, ValueError):
            return ToolResult.error("since_ts must be numeric")
        try:
            limit = max(1, int(args.get("limit", 100)))
        except (TypeError, ValueError):
            return ToolResult.error("limit must be an integer")
        target_agent_id = _target_agent_id(args, self._app_id)
        store = await self._store()
        traces = await _list_traces_since(
            store,
            target_agent_id=target_agent_id,
            since_ts=since_ts,
            limit=limit,
        )
        payload = {
            "target_agent_id": target_agent_id,
            "since_ts": since_ts,
            "limit": limit,
            "traces": traces,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2), **payload)


class GetLatestTraceTool(_RuntimeStoreBackedTool, FunctionTool):
    """Return the latest trace summary in a session."""

    def __init__(
        self,
        session_store: MemoryStore,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        app_id: str,
    ) -> None:
        self._session_store = session_store
        self._app_id = app_id
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
        FunctionTool.__init__(
            self,
            name="get_latest_trace",
            description=(
                "Return the latest trace in a session from the canonical runtime "
                "event store. If session_id is omitted, the latest session is resolved first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional explicit session id. If omitted, the latest "
                            "session is resolved first."
                        ),
                    },
                },
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        session_id, error = await _resolve_session_id(
            self._session_store,
            app_id=self._app_id,
            args=args,
        )
        if error is not None:
            return error

        store = await self._store()
        trace = await store.get_latest_trace(app_id=self._app_id, session_id=session_id)
        if trace is None:
            return ToolResult.error(f"no traces found for session: {session_id}")
        return ToolResult.success(json.dumps(trace, ensure_ascii=True, indent=2), **trace)

class ListRecentTracesTool(_RuntimeStoreBackedTool, FunctionTool):
    """List recent trace summaries for a session."""

    def __init__(
        self,
        session_store: MemoryStore,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        app_id: str,
    ) -> None:
        self._session_store = session_store
        self._app_id = app_id
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
        FunctionTool.__init__(
            self,
            name="list_recent_traces",
            description=(
                "List recent traces in a session from the canonical runtime event "
                "store. If session_id is omitted, the latest session is resolved first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Optional explicit session id. If omitted, the latest "
                            "session is resolved first."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of traces to return (default: 5).",
                        "default": 5,
                    },
                },
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        session_id, error = await _resolve_session_id(
            self._session_store,
            app_id=self._app_id,
            args=args,
        )
        if error is not None:
            return error

        raw_limit = args.get("limit", 5)
        try:
            limit = max(1, int(raw_limit))
        except (TypeError, ValueError):
            return ToolResult.error("limit must be an integer")

        store = await self._store()
        traces = await store.list_recent_traces(
            app_id=self._app_id,
            session_id=session_id,
            limit=limit,
        )
        payload = {
            "session_id": session_id,
            "limit": limit,
            "traces": traces,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2), **payload)


class AppendJsonlTool(FunctionTool):
    """Append one record to a JSONL file."""

    def __init__(self) -> None:
        super().__init__(
            name="append_jsonl",
            description=(
                "Append one JSON object record to a JSONL file. Creates parent "
                "directories when needed. If record.session_id and "
                "record.trace_id are provided and already exist together in the "
                "file, skip the append."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Destination JSONL file path.",
                    },
                    "record": {
                        "type": "object",
                        "description": "JSON object record to append.",
                    },
                },
                "required": ["path", "record"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        raw_path = args.get("path")
        record = args.get("record")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult.error("path is required and must be a non-empty string")
        if not isinstance(record, dict):
            return ToolResult.error("record is required and must be an object")

        path = Path(raw_path).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.error(f"failed to create parent directory: {exc}")

        session_id = str(record.get("session_id") or "").strip()
        trace_id = str(record.get("trace_id") or "").strip()
        if session_id and trace_id and self._has_session_trace_pair(path, session_id, trace_id):
            return ToolResult.success(
                json.dumps({"status": "skipped", "reason": "duplicate_session_trace"}),
                path=str(path),
                appended=False,
                duplicate=True,
                session_id=session_id,
                trace_id=trace_id,
            )

        try:
            encoded = json.dumps(record, ensure_ascii=True, sort_keys=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")
        except (OSError, TypeError, ValueError) as exc:
            return ToolResult.error(f"failed to append JSONL record: {exc}")

        return ToolResult.success(
            json.dumps({"status": "appended", "path": str(path)}),
            path=str(path),
            appended=True,
            duplicate=False,
            session_id=session_id or None,
            trace_id=trace_id or None,
        )

    @staticmethod
    def _has_session_trace_pair(path: Path, session_id: str, trace_id: str) -> bool:
        if not path.exists():
            return False

        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if (
                        str(payload.get("session_id") or "").strip() == session_id
                        and str(payload.get("trace_id") or "").strip() == trace_id
                    ):
                        return True
        except OSError:
            return False
        return False


class TraceDigestWorkflowTool(_RuntimeStoreBackedTool, FunctionTool):
    """Run Masher's workflow-only trace digest contract."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        default_target_agent_id: str,
        trace_digest_jsonl_path: Path,
    ) -> None:
        self._default_target_agent_id = default_target_agent_id
        self._trace_digest_jsonl_path = trace_digest_jsonl_path
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
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
        store = await self._store()
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

        store = await self._store()
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
            _append_digest_jsonl(self._trace_digest_jsonl_path, digest)
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
            "artifact_path": str(self._trace_digest_jsonl_path),
            "appended_trace_count": sum(1 for item in append_results if item),
        }
        return ToolResult.success(json.dumps(next_state, ensure_ascii=True), **next_state)


class OnlineEvalCurationWorkflowTool(_RuntimeStoreBackedTool, FunctionTool):
    """Run Masher's workflow-only online eval curation contract."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore | None,
        runtime_database_url: str | None,
        default_target_agent_id: str,
        online_eval_jsonl_path: Path,
    ) -> None:
        self._default_target_agent_id = default_target_agent_id
        self._online_eval_jsonl_path = online_eval_jsonl_path
        _RuntimeStoreBackedTool.__init__(
            self,
            runtime_store=runtime_store,
            runtime_database_url=runtime_database_url,
        )
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
            default=self._default_target_agent_id,
        )
        session_id = _required_text(workflow_input, "session_id")
        trace_id = _required_text(workflow_input, "trace_id")
        if isinstance(target_agent_id, ToolResult):
            return target_agent_id
        if isinstance(session_id, ToolResult):
            return session_id
        if isinstance(trace_id, ToolResult):
            return trace_id
        store = await self._store()
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
        appended = _append_jsonl_unique(self._online_eval_jsonl_path, row)
        payload = {
            "schema_version": 1,
            "status": "ok",
            "artifact_path": str(self._online_eval_jsonl_path),
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
            default=self._default_target_agent_id,
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

        store = await self._store()
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
            _append_jsonl_unique(self._online_eval_jsonl_path, row) for row in rows
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
            "artifact_path": str(self._online_eval_jsonl_path),
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
) -> TraceBundle:
    events = [
        _serialize_runtime_event(event)
        for event in await store.list_events(
            app_id=target_agent_id,
            session_id=session_id,
            trace_id=trace_id,
            limit=None,
        )
    ]
    if not events:
        raise RuntimeError(
            f"no events found for target/session/trace: {target_agent_id} / {session_id} / {trace_id}"
        )
    failed_events = _failed_events(events)
    token_usage = _sum_token_usage(events)
    started_at = min(float(event["created_at"]) for event in events)
    latest_event_at = max(float(event["created_at"]) for event in events)
    tools_called = _tools_called(events)
    return TraceBundle(
        target_agent_id=target_agent_id,
        session_id=session_id,
        trace_id=trace_id,
        events=events,
        started_at=started_at,
        latest_event_at=latest_event_at,
        duration_ms=round((latest_event_at - started_at) * 1000.0, 3),
        user_message=_extract_user_message(events),
        assistant_response=_extract_assistant_response(events),
        tools_called=tools_called,
        tool_call_count=len(_tool_events(events)),
        tool_error_count=len(
            [
                event
                for event in _tool_events(events)
                if "error" in str(event.get("event_type") or "").lower()
                or "fail" in str(event.get("event_type") or "").lower()
            ]
        ),
        step_count=_step_count(events),
        input_tokens=token_usage["input_tokens"],
        output_tokens=token_usage["output_tokens"],
        failed_events=failed_events,
    )


def _build_trace_digest(bundle: TraceBundle) -> dict[str, Any]:
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


def _build_online_eval_row(bundle: TraceBundle) -> dict[str, Any]:
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


def _tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_events = [
        event
        for event in events
        if ".tool." in str(event.get("event_type") or "")
        or str((event.get("payload") or {}).get("tool_name") or "")
    ]
    return tool_events


def _failed_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if "error" in str(event.get("event_type") or "").lower()
        or "fail" in str(event.get("event_type") or "").lower()
        or str((event.get("payload") or {}).get("status") or "").lower() in {"error", "failed"}
    ]


def _tools_called(events: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for event in _tool_events(events):
        payload = event.get("payload") or {}
        for key in ("tool_name", "tool", "name", "command_name"):
            raw = payload.get(key)
            if isinstance(raw, str) and raw.strip() and raw.strip() not in seen:
                seen.add(raw.strip())
                names.append(raw.strip())
    return names


def _step_count(events: list[dict[str, Any]]) -> int:
    indexes = {
        int(event["loop_index"])
        for event in events
        if event.get("loop_index") is not None
        and "step.completed" in str(event.get("event_type") or "")
    }
    if indexes:
        return len(indexes)
    return len(
        [
            event
            for event in events
            if "step.completed" in str(event.get("event_type") or "")
        ]
    )


def _extract_user_message(events: list[dict[str, Any]]) -> str:
    for event in events:
        payload = event.get("payload") or {}
        raw = payload.get("user_message")
        if isinstance(raw, str) and raw:
            return raw
    for event in events:
        payload = event.get("payload") or {}
        raw = payload.get("message")
        if isinstance(raw, str) and raw:
            return raw
    return ""


def _extract_assistant_response(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        payload = event.get("payload") or {}
        raw = payload.get("agent_response") or payload.get("assistant_response")
        if isinstance(raw, str) and raw:
            return raw
        response = payload.get("response")
        if isinstance(response, dict):
            text = response.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def _sum_token_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for event in events:
        payload = event.get("payload") or {}
        usage = payload.get("token_usage")
        if isinstance(usage, dict):
            input_tokens += _safe_int(usage.get("input") or usage.get("input_tokens"))
            output_tokens += _safe_int(usage.get("output") or usage.get("output_tokens"))
        input_tokens += _safe_int(payload.get("input_tokens"))
        output_tokens += _safe_int(payload.get("output_tokens"))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _target_agent_id(args: dict[str, Any], default: str) -> str:
    raw = args.get("target_agent_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return default


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
    "AppendJsonlTool",
    "GetLatestTraceTool",
    "GetTraceEventsTool",
    "ListTracesSinceTool",
    "ListRecentTracesTool",
    "OnlineEvalCurationWorkflowTool",
    "TraceDigestWorkflowTool",
    "TraceBundle",
    "_build_online_eval_row",
    "_load_trace_bundle",
]

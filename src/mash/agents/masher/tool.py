"""Masher tools for trace-event retrieval and JSONL writes."""

from __future__ import annotations

import json
import os
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
        store = await self._store()
        events = [
            _serialize_runtime_event(item)
            for item in await store.list_events(
                app_id=self._app_id,
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
            "session_id": session_id,
            "trace_id": trace_id,
            "limit": limit,
            "events": events,
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
        session_id, error = await self._resolve_session_id(args)
        if error is not None:
            return error

        store = await self._store()
        trace = await store.get_latest_trace(app_id=self._app_id, session_id=session_id)
        if trace is None:
            return ToolResult.error(f"no traces found for session: {session_id}")
        return ToolResult.success(json.dumps(trace, ensure_ascii=True, indent=2), **trace)

    async def _resolve_session_id(self, args: Dict[str, Any]) -> tuple[str, ToolResult | None]:
        raw_session_id = args.get("session_id")
        if raw_session_id is None:
            latest_session = await self._session_store.get_latest_session(app_id=self._app_id)
            if latest_session is None:
                return "", ToolResult.error("no sessions found for this app")
            return str(latest_session["session_id"]), None
        if isinstance(raw_session_id, str) and raw_session_id.strip():
            return raw_session_id.strip(), None
        return "", ToolResult.error("session_id must be a non-empty string if provided")


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
        resolver = GetLatestTraceTool(
            self._session_store,
            runtime_store=self._runtime_store,
            runtime_database_url=self._runtime_database_url,
            app_id=self._app_id,
        )
        session_id, error = await resolver._resolve_session_id(args)
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


__all__ = [
    "AppendJsonlTool",
    "GetLatestTraceTool",
    "GetTraceEventsTool",
    "ListRecentTracesTool",
]

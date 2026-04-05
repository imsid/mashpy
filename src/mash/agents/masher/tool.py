"""Masher tools for raw log retrieval and JSONL writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ...memory.store import MemoryStore
from ...tools.base import FunctionTool, ToolResult


class GetTraceLogsTool(FunctionTool):
    """Return raw structured log records for a session or a specific trace."""

    def __init__(self, store: MemoryStore, *, app_id: str, store_path: Path) -> None:
        self._store = store
        self._app_id = app_id
        self._store_path = Path(store_path).expanduser()
        super().__init__(
            name="get_trace_logs",
            description=(
                "Fetch raw structured log records from the configured Mash event "
                "store for a session, or for a session plus trace_id pair."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Resolved session id to fetch logs for.",
                    },
                    "trace_id": {
                        "type": "string",
                        "description": (
                            "Optional resolved trace id. If omitted, returns all "
                            "logs for the session."
                        ),
                    },
                },
                "required": ["session_id"],
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        raw_session_id = args.get("session_id")
        raw_trace_id = args.get("trace_id")
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            return ToolResult.error(
                "session_id is required and must be a non-empty string"
            )
        if raw_trace_id is not None and not isinstance(raw_trace_id, str):
            return ToolResult.error("trace_id must be a string if provided")

        session_id = raw_session_id.strip()
        trace_id = raw_trace_id.strip() if isinstance(raw_trace_id, str) else None

        events = [
            _strip_log_cursor(item)
            for item in await self._store.get_logs(
                app_id=self._app_id,
                session_id=session_id,
                trace_id=trace_id,
            )
        ]

        if not events:
            if trace_id is None:
                return ToolResult.error(f"no logs found for session: {session_id}")
            return ToolResult.error(
                f"no logs found for session/trace: {session_id} / {trace_id}"
            )

        payload = {
            "log_path": str(self._store_path),
            "session_id": session_id,
            "trace_id": trace_id,
            "events": events,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2), **payload)


class GetLatestTraceTool(FunctionTool):
    """Return the latest logs-backed trace summary in a session."""

    def __init__(self, store: MemoryStore, *, app_id: str) -> None:
        self._store = store
        self._app_id = app_id
        super().__init__(
            name="get_latest_trace",
            description=(
                "Return the latest trace in a session from the structured event "
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
                },
            },
            _executor=self._execute,
        )

    async def _execute(self, args: Dict[str, Any]) -> ToolResult:
        session_id, error = await self._resolve_session_id(args)
        if error is not None:
            return error

        trace = await self._store.get_latest_log_trace(app_id=self._app_id, session_id=session_id)
        if trace is None:
            return ToolResult.error(f"no traces found for session: {session_id}")
        return ToolResult.success(json.dumps(trace, ensure_ascii=True, indent=2), **trace)

    async def _resolve_session_id(self, args: Dict[str, Any]) -> tuple[str, ToolResult | None]:
        raw_session_id = args.get("session_id")
        if raw_session_id is None:
            latest_session = await self._store.get_latest_session(app_id=self._app_id)
            if latest_session is None:
                return "", ToolResult.error("no sessions found for this app")
            return str(latest_session["session_id"]), None
        if isinstance(raw_session_id, str) and raw_session_id.strip():
            return raw_session_id.strip(), None
        return "", ToolResult.error("session_id must be a non-empty string if provided")


class ListRecentTracesTool(FunctionTool):
    """List recent logs-backed trace summaries for a session."""

    def __init__(self, store: MemoryStore, *, app_id: str) -> None:
        self._store = store
        self._app_id = app_id
        super().__init__(
            name="list_recent_traces",
            description=(
                "List recent traces in a session from the structured event store. "
                "If session_id is omitted, the latest session is resolved first."
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
        resolver = GetLatestTraceTool(self._store, app_id=self._app_id)
        session_id, error = await resolver._resolve_session_id(args)
        if error is not None:
            return error

        raw_limit = args.get("limit", 5)
        try:
            limit = max(1, int(raw_limit))
        except (TypeError, ValueError):
            return ToolResult.error("limit must be an integer")

        traces = await self._store.list_recent_log_traces(
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
    "ListRecentTracesTool",
    "GetTraceLogsTool",
]


def _strip_log_cursor(event: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(event)
    sanitized.pop("log_id", None)
    return sanitized

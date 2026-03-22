"""Masher tools for raw log retrieval and JSONL writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ...tools.base import FunctionTool, ToolResult


class GetTraceLogsTool(FunctionTool):
    """Return raw JSONL log records for a session or a specific trace."""

    def __init__(self, log_file: Path) -> None:
        self._log_file = Path(log_file).expanduser()
        super().__init__(
            name="get_trace_logs",
            description=(
                "Fetch raw JSONL log records from the configured Mash event log "
                "for a session, or for a session plus trace_id pair."
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

    def _execute(self, args: Dict[str, Any]) -> ToolResult:
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

        if not self._log_file.exists():
            return ToolResult.error(f"log file not found: {self._log_file}")

        events: list[dict[str, Any]] = []
        try:
            with self._log_file.open("r", encoding="utf-8") as handle:
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
                    if str(payload.get("session_id") or "").strip() != session_id:
                        continue
                    if trace_id is not None and (
                        str(payload.get("trace_id") or "").strip() != trace_id
                    ):
                        continue
                    events.append(payload)
        except OSError as exc:
            return ToolResult.error(f"failed to read log file: {exc}")

        if not events:
            if trace_id is None:
                return ToolResult.error(f"no logs found for session: {session_id}")
            return ToolResult.error(
                f"no logs found for session/trace: {session_id} / {trace_id}"
            )

        payload = {
            "log_path": str(self._log_file),
            "session_id": session_id,
            "trace_id": trace_id,
            "events": events,
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

    def _execute(self, args: Dict[str, Any]) -> ToolResult:
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
    "GetTraceLogsTool",
]

"""Tools the companion reads its agents' logs with.

Each takes an ``agent_id`` with a ``session_id`` and/or a ``trace_id``, and
every read is keyed by both, on an index or primary key:

- ``list_recent_traces(agent_id, session_id=...)``: ``runtime_request`` by session
- ``get_request_id_for_trace(trace_id, app_id=agent_id)``: the event log's trace index
- ``list_request_events(request_id)`` and ``list_events(agent_id, trace_id=...)``
- ``get_turns(session_id, agent_id)`` and ``get_turn(trace_id, agent_id)``
- ``get_request_identity(request_id)``: ``runtime_request`` by primary key

Before reading, a tool checks that the trace or session belongs to a child of
the companion started from the user's current session.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from mash.logging import get_session_id
from mash.otel.loading import load_request_tree
from mash.otel.projection import project_request
from mash.runtime.events import RuntimeEventType, analyze_projection, serialize_runtime_event
from mash.tools.base import ToolResult

TURN_CHARS = 8000
PAYLOAD_CHARS = 2000
DEFAULT_EVENTS = 50
MAX_EVENTS = 200
DEFAULT_TRACES = 10
MAX_TRACES = 50
DEFAULT_TURNS = 5
MAX_TURNS = 20


def _clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _limit(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None and not isinstance(value, bool) else default
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


class AgentLogReader:
    """The stores the tools read, bound when the companion's process starts."""

    def __init__(self, owner_agent_id: str) -> None:
        self.owner_agent_id = owner_agent_id
        self.runtime_store: Any = None
        self.memory_store: Any = None

    def bind(self, runtime_store: Any, memory_store: Any) -> None:
        self.runtime_store = runtime_store
        self.memory_store = memory_store

    async def check_request(self, request_id: Optional[str], agent_id: str) -> Optional[str]:
        """None when ``request_id`` is ``agent_id``'s child of the owner in this session."""
        refused = "that trace or session is not a child of yours in this conversation"
        if not request_id:
            return refused
        child = await self.runtime_store.get_request_identity(request_id)
        if child is None or child.agent_id != agent_id or not child.parent_request_id:
            return refused
        parent = await self.runtime_store.get_request_identity(child.parent_request_id)
        if (
            parent is None
            or parent.agent_id != self.owner_agent_id
            or parent.session_id != get_session_id()
        ):
            return refused
        return None

    async def check_session(self, agent_id: str, session_id: str) -> tuple[list, Optional[str]]:
        """The session's latest traces, newest first, and a refusal if it is not ours."""
        traces = await self.runtime_store.list_recent_traces(
            agent_id, session_id=session_id, limit=MAX_TRACES
        )
        if not traces:
            return [], f"no requests of '{agent_id}' in session '{session_id}'"
        return traces, await self.check_request(traces[0].get("request_id"), agent_id)

    async def check_trace(self, agent_id: str, trace_id: str) -> tuple[Optional[str], Optional[str]]:
        """The request behind the trace, and a refusal if it is not ours."""
        request_id = await self.runtime_store.get_request_id_for_trace(trace_id, app_id=agent_id)
        if request_id is None:
            return None, f"no trace '{trace_id}' of '{agent_id}'"
        return request_id, await self.check_request(request_id, agent_id)


def _required(args: Dict[str, Any], key: str) -> str:
    return str(args.get(key) or "").strip()


class _LogTool:
    name = ""
    description = ""
    parameters: Dict[str, Any] = {}
    requires_approval = False
    parallel_safe = True

    def __init__(self, reader: AgentLogReader) -> None:
        self._reader = reader

    def to_llm_format(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    @staticmethod
    def _json(value: Any) -> ToolResult:
        return ToolResult.success(json.dumps(value, ensure_ascii=False, default=str))


class ListAgentTracesTool(_LogTool):
    name = "ListAgentTraces"
    description = (
        "List the most recent requests of one agent in one of its sessions from "
        "your ledger: trace_id, request_id, status, start time, duration and "
        "the first line of the request, newest first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "description": f"Defaults to {DEFAULT_TRACES}."},
        },
        "required": ["agent_id", "session_id"],
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        agent_id, session_id = _required(args, "agent_id"), _required(args, "session_id")
        if not agent_id or not session_id:
            return ToolResult.error("agent_id and session_id are required")
        traces, refused = await self._reader.check_session(agent_id, session_id)
        if refused:
            return ToolResult.error(refused)
        limit = _limit(args.get("limit"), DEFAULT_TRACES, MAX_TRACES)
        traces = traces[:limit]
        turns = await self._reader.memory_store.get_turns(
            session_id=session_id, app_id=agent_id, limit=limit
        )
        asked = {turn["trace_id"]: turn.get("user_message") or "" for turn in turns}
        return self._json(
            [
                {
                    "trace_id": trace["trace_id"],
                    "request_id": trace.get("request_id"),
                    "status": trace.get("status"),
                    "started_at": trace.get("started_at"),
                    "duration_ms": int(
                        (float(trace.get("latest_event_at") or 0) - float(trace.get("started_at") or 0))
                        * 1000
                    ),
                    "request": (asked.get(trace["trace_id"]) or "").strip().split("\n", 1)[0][:200],
                }
                for trace in traces
            ]
        )


class InspectAgentTraceTool(_LogTool):
    name = "InspectAgentTrace"
    description = (
        "Read one trace of an agent. view='summary' (the default) gives its "
        "status, error, timing, tool call and error counts, steps, slowest "
        "operations and token usage. view='events' gives its events newest "
        "first, filtered by event_type_prefix (for example 'execute_tool' for "
        "tool calls) and capped by limit."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "trace_id": {"type": "string"},
            "view": {"type": "string", "enum": ["summary", "events"]},
            "event_type_prefix": {"type": "string"},
            "limit": {
                "type": "integer",
                "description": f"Events to return; defaults to {DEFAULT_EVENTS}, at most {MAX_EVENTS}.",
            },
        },
        "required": ["agent_id", "trace_id"],
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        agent_id, trace_id = _required(args, "agent_id"), _required(args, "trace_id")
        if not agent_id or not trace_id:
            return ToolResult.error("agent_id and trace_id are required")
        view = str(args.get("view") or "summary")
        if view not in {"summary", "events"}:
            return ToolResult.error("view must be 'summary' or 'events'")
        request_id, refused = await self._reader.check_trace(agent_id, trace_id)
        if refused:
            return ToolResult.error(refused)
        store = self._reader.runtime_store
        if view == "summary":
            events, children = await load_request_tree(store, request_id)
            analysis = analyze_projection(project_request(events, children=children))
            end = next(
                (e for e in reversed(events) if e.event_type == RuntimeEventType.INVOKE_AGENT_END.value),
                None,
            )
            error = (end.payload or {}).get("error") if end is not None else None
            return self._json(
                {
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "status": analysis.status,
                    "error": _clip(error, PAYLOAD_CHARS) if error else None,
                    "total_duration_ms": round(analysis.total_duration_ms, 3),
                    "tool_error_count": analysis.tool_error_count,
                    **analysis.to_digest_dict(),
                }
            )
        prefix = str(args.get("event_type_prefix") or "").strip() or None
        events = await store.list_events(
            agent_id,
            trace_id=trace_id,
            event_type_prefix=prefix,
            limit=_limit(args.get("limit"), DEFAULT_EVENTS, MAX_EVENTS),
        )
        rows = []
        for event in sorted(events, key=lambda e: int(e.event_id or 0), reverse=True):
            row = serialize_runtime_event(event)
            row["payload"] = _clip(row.get("payload") or {}, PAYLOAD_CHARS)
            rows.append(row)
        return self._json(rows)


class GetAgentTurnsTool(_LogTool):
    name = "GetAgentTurns"
    description = (
        "Read what an agent was asked and what it answered: with trace_id, "
        "that one turn; with session_id alone, the session's latest turns, "
        "newest first. A running or failed request has no turn; read its "
        "trace with InspectAgentTrace instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "session_id": {"type": "string"},
            "trace_id": {"type": "string"},
            "limit": {"type": "integer", "description": f"Defaults to {DEFAULT_TURNS}."},
        },
        "required": ["agent_id"],
    }

    @staticmethod
    def _turn(turn: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "trace_id": turn["trace_id"],
            "created_at": turn.get("created_at"),
            "user_message": _clip(turn.get("user_message") or "", TURN_CHARS),
            "agent_response": _clip(turn.get("agent_response") or "", TURN_CHARS),
        }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        agent_id = _required(args, "agent_id")
        session_id = _required(args, "session_id") or None
        trace_id = _required(args, "trace_id") or None
        if not agent_id:
            return ToolResult.error("agent_id is required")
        if session_id is None and trace_id is None:
            return ToolResult.error("give a session_id or a trace_id")
        memory = self._reader.memory_store
        if trace_id is not None:
            _, refused = await self._reader.check_trace(agent_id, trace_id)
            if refused:
                return ToolResult.error(refused)
            turn = await memory.get_turn(trace_id, agent_id, session_id=session_id)
            if turn is None:
                return ToolResult.error(
                    f"trace '{trace_id}' has no turn: the request is running or did "
                    "not complete. Read it with InspectAgentTrace."
                )
            return self._json([self._turn(turn)])
        _, refused = await self._reader.check_session(agent_id, session_id)
        if refused:
            return ToolResult.error(refused)
        turns = await memory.get_turns(
            session_id=session_id,
            app_id=agent_id,
            limit=_limit(args.get("limit"), DEFAULT_TURNS, MAX_TURNS),
        )
        if not turns:
            return ToolResult.error(
                f"session '{session_id}' has no turns yet: its requests are running "
                "or did not complete. Read them with InspectAgentTrace."
            )
        return self._json([self._turn(turn) for turn in reversed(turns)])


__all__ = [
    "AgentLogReader",
    "GetAgentTurnsTool",
    "InspectAgentTraceTool",
    "ListAgentTracesTool",
]

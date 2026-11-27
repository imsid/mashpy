"""read_board and search_board: a conversation's board, read by number or by words.

A request the companion routed gets both. Each reads the request's own
session, bound when the request's agent is built; neither takes a session id,
so an agent reads only its own conversation's board. Neither reads another
request's event log: an agent sees what other agents wrote as their final
entries.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..memory.store import MemoryStore
from .base import ToolResult

READ_DEFAULT = 20
READ_MAX = 50
SEARCH_DEFAULT = 10
SEARCH_MAX = 25


def _number(args: Dict[str, Any], key: str) -> Optional[int] | str:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return f"{key} must be an integer"
    try:
        return int(value)
    except (TypeError, ValueError):
        return f"{key} must be an integer"


class _BoardTool:
    name = ""
    description = ""
    parameters: Dict[str, Any] = {}
    requires_approval = False
    parallel_safe = True

    def __init__(self, store: MemoryStore, session_id: str) -> None:
        if not str(session_id or "").strip():
            raise ValueError("session_id is required")
        self._store = store
        self._session_id = str(session_id).strip()

    def to_llm_format(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class ReadBoardTool(_BoardTool):
    name = "read_board"
    description = (
        "Read entries of this conversation's board by number, in full: each "
        "has its number (seq), the agent that wrote it, the user's message and "
        "the reply. Use it for entries a summary replaced, or for what was "
        "added after a given entry. after and before are entry numbers and "
        "are not included; with only before, you get the entries just before "
        f"it. limit defaults to {READ_DEFAULT}, at most {READ_MAX}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "after": {"type": "integer", "description": "Entries with a number above this."},
            "before": {"type": "integer", "description": "Entries with a number below this."},
            "limit": {"type": "integer"},
        },
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        after, before, limit = (_number(args, key) for key in ("after", "before", "limit"))
        for value in (after, before, limit):
            if isinstance(value, str):
                return ToolResult.error(value)
        limit = READ_DEFAULT if limit is None else max(1, min(int(limit), READ_MAX))
        entries = await self._store.read_entries(
            self._session_id, after=after, before=before, limit=limit
        )
        return ToolResult.success(
            json.dumps(
                [
                    {
                        "seq": entry["seq"],
                        "agent_id": entry["agent_id"],
                        "type": (entry.get("metadata") or {}).get("type") or "turn",
                        "user_message": entry["user_message"],
                        "agent_response": entry["agent_response"],
                    }
                    for entry in entries
                ],
                ensure_ascii=False,
            )
        )


class SearchBoardTool(_BoardTool):
    name = "search_board"
    description = (
        "Search this conversation's board for entries whose message or reply "
        "has the query's words. Returns each match's number (seq), the agent "
        "that wrote it and a snippet, best first. Read a match in full with "
        "read_board."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {
                "type": "integer",
                "description": f"Defaults to {SEARCH_DEFAULT}, at most {SEARCH_MAX}.",
            },
        },
        "required": ["query"],
    }

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult.error("query is required")
        limit = _number(args, "limit")
        if isinstance(limit, str):
            return ToolResult.error(limit)
        limit = SEARCH_DEFAULT if limit is None else max(1, min(int(limit), SEARCH_MAX))
        results = await self._store.search_entries(self._session_id, query, limit=limit)
        return ToolResult.success(
            json.dumps(
                [
                    {"seq": hit["seq"], "agent_id": hit["agent_id"], "snippet": hit["snippet"]}
                    for hit in results
                ],
                ensure_ascii=False,
            )
        )


def board_tools(store: MemoryStore, session_id: str) -> list[_BoardTool]:
    """The board tools over ``session_id``: the request's own session."""
    return [ReadBoardTool(store, session_id), SearchBoardTool(store, session_id)]


__all__ = ["ReadBoardTool", "SearchBoardTool", "board_tools"]

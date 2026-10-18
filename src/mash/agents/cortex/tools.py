"""Cortex reads on an agent's computer: the profile block and ``search_cortex``.

Both are scoped to the request's ``user_id``, taken from the request and never
from the model's arguments. A request without a user gets neither.
"""

from __future__ import annotations

import json
from typing import Any

from mash.tools.base import FunctionTool, Tool, ToolResult

from .models import CortexSettings
from .store import CortexStore

SEARCH_CORTEX_TOOL_NAME = "search_cortex"
_TRUNCATION_MARKER = "\n[profile truncated]"


def profile_block(profile: str, max_chars: int) -> str:
    """The system prompt block for a user's profile, or '' when it is empty."""
    text = str(profile or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + _TRUNCATION_MARKER
    return (
        "ABOUT THIS USER\n"
        "What you know about the user from earlier conversations. Use "
        f"{SEARCH_CORTEX_TOOL_NAME} for details.\n\n{text}"
    )


class CortexReader:
    """A runtime's read access to Cortex: one store, the pool's settings."""

    def __init__(self, store: CortexStore, settings: CortexSettings) -> None:
        self.store = store
        self.settings = settings

    async def open(self) -> None:
        await self.store.open()

    async def close(self) -> None:
        await self.store.close()

    async def profile_block(self, user_id: str) -> str:
        profile = await self.store.get_profile(user_id)
        return profile_block(profile or "", self.settings.profile_max_chars)

    def search_tool(self, user_id: str) -> Tool:
        store = self.store

        async def execute(args: dict[str, Any]) -> ToolResult:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return ToolResult.error("query is required and must be a non-empty string")
            try:
                limit = int(args.get("limit", 10))
            except (TypeError, ValueError):
                return ToolResult.error("limit must be an integer")
            entries = await store.search_entries(user_id, query, limit=max(1, limit))
            payload = {
                "query": query.strip(),
                "entries": [
                    {
                        "topic": entry.topic,
                        "content": entry.content,
                        "updated_at": entry.updated_at,
                        "sources": entry.sources,
                    }
                    for entry in entries
                ],
            }
            return ToolResult(content=json.dumps(payload, indent=2), is_error=False)

        return FunctionTool(
            name=SEARCH_CORTEX_TOOL_NAME,
            description=(
                "Search what you know about the user from earlier conversations: "
                "preferences, accounts, ongoing work and standing instructions. "
                "Returns matching entries with their topic and content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            _executor=execute,
        )


__all__ = ["CortexReader", "SEARCH_CORTEX_TOOL_NAME", "profile_block"]

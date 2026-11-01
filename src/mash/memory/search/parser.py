"""Query parsing for memory search DSL."""

from __future__ import annotations

import re

from .types import ParsedSearchQuery, SearchColumn

_KNOWN_PREFIXES: dict[str, SearchColumn] = {
    "@user:": "user_message",
    "@agent:": "agent_response",
}
_UNKNOWN_PREFIX_RE = re.compile(r"^@([a-zA-Z_][a-zA-Z0-9_-]*):")


class QueryParser:
    """Parse and normalize memory search queries."""

    async def parse(
        self,
        query: str,
    ) -> ParsedSearchQuery:
        """Parse `@user:` / `@agent:` DSL and normalize query text."""
        raw_query = str(query)
        trimmed = raw_query.strip()
        if not trimmed:
            raise ValueError("Search query cannot be empty")

        lowered = trimmed.lower()
        selected_prefix: str | None = None
        column: SearchColumn | None = None
        for prefix, parsed_column in _KNOWN_PREFIXES.items():
            if lowered.startswith(prefix):
                selected_prefix = prefix
                column = parsed_column
                break

        if selected_prefix is None:
            if _UNKNOWN_PREFIX_RE.match(trimmed):
                raise ValueError(
                    "Unknown search target prefix. Use '@user:' or '@agent:'."
                )
            raise ValueError("Search query must start with '@user:' or '@agent:'.")
        assert column is not None

        query_term = self._normalize_term(trimmed[len(selected_prefix) :])
        if not query_term:
            raise ValueError("Search query term cannot be empty")

        parsed = ParsedSearchQuery(
            raw_query=raw_query,
            column=column,
            query_term=query_term,
            keyword_query=query_term,
            semantic_query_text=query_term,
            semantic_embedding=None,
        )
        return parsed

    @staticmethod
    def _normalize_term(value: str) -> str:
        """Trim and collapse internal whitespace."""
        return " ".join(value.split())

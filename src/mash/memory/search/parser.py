"""Query parsing for memory search DSL."""

from __future__ import annotations

import re
import time
from typing import Any

from ...logging import EventLogger, MemorySearchEvent
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
        query_id: str,
        event_logger: EventLogger,
        *,
        app_id: str | None = None,
        session_id: str | None = None,
    ) -> ParsedSearchQuery:
        """Parse `@user:` / `@agent:` DSL and normalize query text."""
        start = time.time()
        try:
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
            await self._emit_event(
                event_logger=event_logger,
                event_type="memory.search.parse.complete",
                query_id=query_id,
                app_id=app_id,
                session_id=session_id,
                level="INFO",
                duration_ms=self._elapsed_ms(start),
                metadata={
                    "column": parsed.column,
                    "query_term": parsed.query_term,
                },
            )
            return parsed
        except Exception as exc:
            await self._emit_event(
                event_logger=event_logger,
                event_type="memory.search.parse.error",
                query_id=query_id,
                app_id=app_id,
                session_id=session_id,
                level="ERROR",
                duration_ms=self._elapsed_ms(start),
                error=str(exc),
            )
            raise

    @staticmethod
    def _normalize_term(value: str) -> str:
        """Trim and collapse internal whitespace."""
        return " ".join(value.split())

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        return int((time.time() - start_time) * 1000)

    @staticmethod
    async def _emit_event(
        *,
        event_logger: EventLogger | None,
        event_type: str,
        query_id: str | None,
        app_id: str | None,
        session_id: str | None,
        level: str,
        duration_ms: int | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if event_logger is None or query_id is None or app_id is None:
            return
        await event_logger.emit(
            MemorySearchEvent(
                event_type=event_type,
                app_id=app_id,
                session_id=session_id,
                query_id=query_id,
                level=level,
                stage="parse",
                duration_ms=duration_ms,
                error=error,
                metadata=metadata,
            )
        )

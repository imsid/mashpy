"""Retrieval stage for memory search."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ...logging import EventLogger, MemorySearchEvent
from ..store import MemoryStore
from .types import MAX_PREVIEW_CHARS, ParsedSearchQuery, RetrievalConfig, RetrievalHit


def _sanitize_preview(preview: Any) -> str:
    """Coerce preview to plain text and cap length."""
    text = "" if preview is None else str(preview)
    return text[:MAX_PREVIEW_CHARS]


def _validate_score(value: Any) -> float:
    """Validate normalized score contract."""
    score = float(value)
    if score < 0.0 or score > 1.0:
        raise ValueError(f"Retrieval score must be in [0, 1], got {score}")
    return score


def _coerce_limit(limit: int) -> int:
    """Normalize requested result limit."""
    return max(0, int(limit))


def _to_hit(
    raw_hit: dict[str, Any],
    *,
    method: str,
    column: str,
    rank: int,
) -> RetrievalHit:
    """Convert a store hit dict to a typed retrieval hit."""
    trace_id = str(raw_hit["trace_id"])
    session_id = str(raw_hit["session_id"])
    score = _validate_score(raw_hit["score"])
    preview = _sanitize_preview(raw_hit.get("preview", ""))
    return RetrievalHit(
        trace_id=trace_id,
        session_id=session_id,
        score=score,
        preview=preview,
        column=column,  # type: ignore[arg-type]
        method=method,  # type: ignore[arg-type]
        rank=rank,
    )


class KeywordRetriever:
    """Adapter for keyword-based retrieval using the store protocol."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def retrieve(
        self,
        parsed: ParsedSearchQuery,
        *,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[RetrievalHit]:
        """Retrieve keyword hits from the configured store backend."""
        normalized_limit = _coerce_limit(limit)
        if normalized_limit <= 0:
            return []

        raw_hits = await self._store.keyword_search(
            column=parsed.column,
            query_term=parsed.keyword_query,
            limit=normalized_limit,
            session_id=session_id,
            app_id=app_id,
        )
        return [
            _to_hit(raw_hit, method="keyword", column=parsed.column, rank=index)
            for index, raw_hit in enumerate(raw_hits, start=1)
        ]


class SemanticRetriever:
    """Adapter for semantic retrieval using the store protocol."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def retrieve(
        self,
        parsed: ParsedSearchQuery,
        *,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[RetrievalHit]:
        """Retrieve semantic hits from the configured store backend."""
        normalized_limit = _coerce_limit(limit)
        if normalized_limit <= 0:
            return []

        raw_hits = await self._store.semantic_search(
            column=parsed.column,
            query_term=parsed.semantic_query_text,
            query_embedding=parsed.semantic_embedding,
            limit=normalized_limit,
            session_id=session_id,
            app_id=app_id,
        )
        return [
            _to_hit(raw_hit, method="semantic", column=parsed.column, rank=index)
            for index, raw_hit in enumerate(raw_hits, start=1)
        ]


@dataclass(frozen=True)
class HybridRetrievalOutputs:
    """Outputs of the retrieval orchestrator."""

    keyword_hits: list[RetrievalHit]
    semantic_hits: list[RetrievalHit]


class HybridRetrievalOrchestrator:
    """Run enabled retrieval methods and return their results."""

    def __init__(
        self,
        keyword_retriever: KeywordRetriever,
        semantic_retriever: SemanticRetriever,
        *,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._keyword_retriever = keyword_retriever
        self._semantic_retriever = semantic_retriever
        self._config = config or RetrievalConfig()
        self._validate_enabled()

    async def retrieve(
        self,
        parsed: ParsedSearchQuery,
        query_id: str,
        event_logger: EventLogger,
        *,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> HybridRetrievalOutputs:
        """Run enabled retrieval methods and return their independent outputs."""
        start = time.time()
        self._validate_enabled()
        keyword_hits: list[RetrievalHit] = []
        semantic_hits: list[RetrievalHit] = []
        try:
            if self._config.enable_keyword:
                keyword_hits = await self._keyword_retriever.retrieve(
                    parsed,
                    limit=limit,
                    session_id=session_id,
                    app_id=app_id,
                )

            if self._config.enable_semantic:
                semantic_hits = await self._semantic_retriever.retrieve(
                    parsed,
                    limit=limit,
                    session_id=session_id,
                    app_id=app_id,
                )

            outputs = HybridRetrievalOutputs(
                keyword_hits=keyword_hits,
                semantic_hits=semantic_hits,
            )
            await self._emit_event(
                event_logger=event_logger,
                event_type="memory.search.retrieval.complete",
                query_id=query_id,
                app_id=app_id,
                session_id=session_id,
                level="INFO",
                duration_ms=self._elapsed_ms(start),
                metadata={
                    "keyword_hits": len(outputs.keyword_hits),
                    "semantic_hits": len(outputs.semantic_hits),
                    "keyword_enabled": self._config.enable_keyword,
                    "semantic_enabled": self._config.enable_semantic,
                },
            )
            return outputs
        except Exception as exc:
            await self._emit_event(
                event_logger=event_logger,
                event_type="memory.search.retrieval.error",
                query_id=query_id,
                app_id=app_id,
                session_id=session_id,
                level="ERROR",
                duration_ms=self._elapsed_ms(start),
                error=str(exc),
            )
            raise

    def _validate_enabled(self) -> None:
        """Require at least one retrieval method to be enabled."""
        if not (self._config.enable_keyword or self._config.enable_semantic):
            raise ValueError("At least one retrieval method must be enabled")

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
                stage="retrieval",
                duration_ms=duration_ms,
                error=error,
                metadata=metadata,
            )
        )

"""High-level memory search service."""

from __future__ import annotations

import time
import uuid
from typing import Any

from ...logging import EventLogger, MemorySearchEvent
from ..store import MemoryStore
from .parser import QueryParser
from .rerank import WeightedFusionReranker
from .retrieval import (
    HybridRetrievalOrchestrator,
    KeywordRetriever,
    SemanticRetriever,
)
from .types import FusionWeights, RetrievalConfig, SearchResult


class MemorySearchService:
    """Three-stage hybrid search over memory turns."""

    def __init__(
        self,
        store: MemoryStore,
        event_logger: EventLogger,
        *,
        parser: QueryParser | None = None,
        retrieval_orchestrator: HybridRetrievalOrchestrator | None = None,
        reranker: WeightedFusionReranker | None = None,
        retrieval_config: RetrievalConfig | None = None,
        fusion_weights: FusionWeights | None = None,
    ) -> None:
        self._parser = parser or QueryParser()
        self._retrieval_orchestrator = (
            retrieval_orchestrator
            or HybridRetrievalOrchestrator(
                KeywordRetriever(store),
                SemanticRetriever(store),
                config=retrieval_config,
            )
        )
        self._reranker = reranker or WeightedFusionReranker(fusion_weights)
        self._event_logger = event_logger

    async def search(
        self,
        query: str,
        *,
        app_id: str,
        limit: int = 10,
        session_id: str | None = None,
    ) -> list[SearchResult]:
        """Return ranked results with trace_id, session_id, score, and preview."""
        query_id = uuid.uuid4().hex
        overall_start = time.time()
        normalized_limit = max(0, int(limit))
        if normalized_limit <= 0:
            await self._emit_search_event(
                event_type="memory.search.complete",
                app_id=app_id,
                session_id=session_id,
                query_id=query_id,
                level="INFO",
                stage="service",
                duration_ms=self._elapsed_ms(overall_start),
                metadata={
                    "result_count": 0,
                    "limit_applied": normalized_limit,
                    "short_circuit": "non_positive_limit",
                },
            )
            return []

        parsed_query = await self._parser.parse(
            query,
            event_logger=self._event_logger,
            query_id=query_id,
            app_id=app_id,
            session_id=session_id,
        )
        await self._emit_search_event(
            event_type="memory.search.start",
            app_id=app_id,
            session_id=session_id,
            query_id=query_id,
            level="INFO",
            stage="service",
            metadata={
                "query_term": parsed_query.query_term,
                "column": parsed_query.column,
                "limit_requested": limit,
                "limit_normalized": normalized_limit,
                "has_session_id": session_id is not None,
            },
        )

        retrieved = await self._retrieval_orchestrator.retrieve(
            parsed_query,
            limit=normalized_limit,
            session_id=session_id,
            app_id=app_id,
            event_logger=self._event_logger,
            query_id=query_id,
        )
        fused_hits = await self._reranker.rerank(
            keyword_hits=retrieved.keyword_hits,
            semantic_hits=retrieved.semantic_hits,
            event_logger=self._event_logger,
            query_id=query_id,
            app_id=app_id,
            session_id=session_id,
        )

        results = [
            SearchResult(
                trace_id=hit.trace_id,
                session_id=hit.session_id,
                similarity_score=hit.final_score,
                preview=hit.preview,
            )
            for hit in fused_hits[:normalized_limit]
        ]
        await self._emit_search_event(
            event_type="memory.search.complete",
            app_id=app_id,
            session_id=session_id,
            query_id=query_id,
            level="INFO",
            stage="service",
            duration_ms=self._elapsed_ms(overall_start),
            metadata={
                "result_count": len(results),
                "limit_applied": normalized_limit,
            },
        )
        return results

    async def _emit_search_event(
        self,
        *,
        event_type: str,
        app_id: str,
        session_id: str | None,
        query_id: str,
        level: str,
        stage: str,
        duration_ms: int | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._event_logger is None:
            return
        await self._event_logger.emit(
            MemorySearchEvent(
                event_type=event_type,
                app_id=app_id,
                session_id=session_id,
                query_id=query_id,
                level=level,
                stage=stage,
                duration_ms=duration_ms,
                error=error,
                metadata=metadata,
            )
        )

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        return int((time.time() - start_time) * 1000)

"""High-level memory search service."""

from __future__ import annotations

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

    async def search(
        self,
        query: str,
        *,
        app_id: str,
        limit: int = 10,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[SearchResult]:
        """Return ranked results with trace_id, session_id, score, and preview.

        ``user_id`` limits results to that user's turns.
        """
        normalized_limit = max(0, int(limit))
        if normalized_limit <= 0:
            return []

        parsed_query = await self._parser.parse(query)

        retrieved = await self._retrieval_orchestrator.retrieve(
            parsed_query,
            limit=normalized_limit,
            session_id=session_id,
            app_id=app_id,
            user_id=user_id,
        )
        fused_hits = await self._reranker.rerank(
            keyword_hits=retrieved.keyword_hits,
            semantic_hits=retrieved.semantic_hits,
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
        return results

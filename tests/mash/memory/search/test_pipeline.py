"""Unit tests for phase-1 memory search pipeline."""

from __future__ import annotations

import unittest

from mash.memory.search.parser import QueryParser
from mash.memory.search.rerank import WeightedFusionReranker
from mash.memory.search.retrieval import (
    HybridRetrievalOrchestrator,
    KeywordRetriever,
    SemanticRetriever,
)
from mash.memory.search.service import MemorySearchService
from mash.memory.search.types import RetrievalConfig, RetrievalHit, SearchResult


class FakeSearchStore:
    """Minimal fake store implementing search methods for tests."""

    def __init__(self) -> None:
        self.keyword_hits: list[dict[str, object]] = []
        self.semantic_hits: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []

    async def keyword_search(
        self,
        column: str,
        query_term: str,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "method": "keyword",
                "column": column,
                "query_term": query_term,
                "limit": limit,
                "session_id": session_id,
                "app_id": app_id,
                "user_id": user_id,
            }
        )
        return self.keyword_hits

    async def semantic_search(
        self,
        column: str,
        query_term: str,
        query_embedding: list[float] | None,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "method": "semantic",
                "column": column,
                "query_term": query_term,
                "query_embedding": query_embedding,
                "limit": limit,
                "session_id": session_id,
                "app_id": app_id,
                "user_id": user_id,
            }
        )
        return self.semantic_hits


class FailingKeywordStore(FakeSearchStore):
    """Store that raises during keyword retrieval."""

    async def keyword_search(
        self,
        column: str,
        query_term: str,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        raise RuntimeError("keyword retrieval failed")


class QueryParserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.parser = QueryParser()

    async def _parse(self, query: str):
        return await self.parser.parse(query)

    async def test_parses_user_query(self) -> None:
        parsed = await self._parse("@user:hello")
        self.assertEqual(parsed.column, "user_message")
        self.assertEqual(parsed.query_term, "hello")
        self.assertIsNone(parsed.semantic_embedding)

    async def test_parses_agent_query_and_collapses_whitespace(self) -> None:
        parsed = await self._parse("  @agent:  hello   world  ")
        self.assertEqual(parsed.column, "agent_response")
        self.assertEqual(parsed.query_term, "hello world")
        self.assertEqual(parsed.keyword_query, "hello world")
        self.assertEqual(parsed.semantic_query_text, "hello world")

    async def test_rejects_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            await self._parse("   ")

    async def test_rejects_unprefixed_query(self) -> None:
        with self.assertRaises(ValueError):
            await self._parse("hello")

    async def test_rejects_unknown_prefix(self) -> None:
        with self.assertRaises(ValueError):
            await self._parse("@foo:bar")

    async def test_rejects_empty_query_term(self) -> None:
        with self.assertRaises(ValueError):
            await self._parse("@user:   ")


class RetrievalOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeSearchStore()
        self.parser = QueryParser()
        self.keyword_retriever = KeywordRetriever(self.store)  # type: ignore[arg-type]
        self.semantic_retriever = SemanticRetriever(self.store)  # type: ignore[arg-type]

    async def _parse(self, query: str):
        return await self.parser.parse(query)

    async def test_keyword_only_calls_only_keyword_search(self) -> None:
        self.store.keyword_hits = [
            {"trace_id": "t1", "session_id": "s1", "score": 0.8, "preview": "hello"},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
            config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        )

        parsed = await self._parse("@user:hello")
        outputs = await orchestrator.retrieve(
            parsed,
            limit=5,
        )

        self.assertEqual(len(outputs.keyword_hits), 1)
        self.assertEqual(outputs.keyword_hits[0].trace_id, "t1")
        self.assertEqual(outputs.keyword_hits[0].session_id, "s1")
        self.assertEqual(outputs.keyword_hits[0].preview, "hello")
        self.assertEqual(outputs.semantic_hits, [])
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "keyword")
        self.assertEqual(self.store.calls[0]["column"], "user_message")

    async def test_semantic_only_calls_only_semantic_search(self) -> None:
        self.store.semantic_hits = [
            {"trace_id": "t2", "session_id": "s2", "score": 0.9, "preview": "answer"},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
            config=RetrievalConfig(enable_keyword=False, enable_semantic=True),
        )

        parsed = await self._parse("@agent:answer")
        outputs = await orchestrator.retrieve(
            parsed,
            limit=3,
        )

        self.assertEqual(outputs.keyword_hits, [])
        self.assertEqual(len(outputs.semantic_hits), 1)
        self.assertEqual(outputs.semantic_hits[0].column, "agent_response")
        self.assertEqual(outputs.semantic_hits[0].method, "semantic")
        self.assertEqual(outputs.semantic_hits[0].session_id, "s2")
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "semantic")
        self.assertEqual(self.store.calls[0]["column"], "agent_response")
        self.assertIsNone(self.store.calls[0]["query_embedding"])

    def test_both_disabled_raises(self) -> None:
        with self.assertRaises(ValueError):
            HybridRetrievalOrchestrator(
                self.keyword_retriever,
                self.semantic_retriever,
                config=RetrievalConfig(enable_keyword=False, enable_semantic=False),
            )

    async def test_retrieval_caps_preview_length(self) -> None:
        self.store.keyword_hits = [
            {"trace_id": "t1", "session_id": "s1", "score": 0.5, "preview": "x" * 300},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
        )

        parsed = await self._parse("@user:x")
        outputs = await orchestrator.retrieve(
            parsed,
            limit=2,
        )
        self.assertEqual(len(outputs.keyword_hits[0].preview), 200)


class WeightedFusionRerankerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.reranker = WeightedFusionReranker()

    async def test_fuses_overlapping_hits_with_weighted_scores(self) -> None:
        keyword_hits = [
            RetrievalHit(
                trace_id="t1",
                session_id="s1",
                score=0.5,
                preview="keyword preview",
                column="user_message",
                method="keyword",
                rank=1,
            )
        ]
        semantic_hits = [
            RetrievalHit(
                trace_id="t1",
                session_id="s1",
                score=0.8,
                preview="semantic preview",
                column="user_message",
                method="semantic",
                rank=1,
            )
        ]

        fused = await self.reranker.rerank(
            keyword_hits=keyword_hits,
            semantic_hits=semantic_hits,
        )

        self.assertEqual(len(fused), 1)
        self.assertAlmostEqual(fused[0].final_score, 0.71)
        self.assertEqual(fused[0].preview, "keyword preview")
        self.assertEqual(fused[0].session_id, "s1")
        self.assertAlmostEqual(fused[0].semantic_score, 0.8)
        self.assertAlmostEqual(fused[0].keyword_score, 0.5)

    async def test_handles_missing_method_scores(self) -> None:
        fused = await self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    trace_id="t1",
                    session_id="s1",
                    score=0.4,
                    preview="k",
                    column="user_message",
                    method="keyword",
                    rank=1,
                )
            ],
            semantic_hits=[
                RetrievalHit(
                    trace_id="t2",
                    session_id="s2",
                    score=0.6,
                    preview="s",
                    column="user_message",
                    method="semantic",
                    rank=1,
                )
            ],
        )

        self.assertEqual([hit.trace_id for hit in fused], ["t2", "t1"])
        self.assertAlmostEqual(fused[0].final_score, 0.42)
        self.assertAlmostEqual(fused[1].final_score, 0.12)

    async def test_tie_breaks_by_trace_id(self) -> None:
        fused = await self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    trace_id="b",
                    session_id="s1",
                    score=0.3,
                    preview="b",
                    column="user_message",
                    method="keyword",
                    rank=1,
                ),
                RetrievalHit(
                    trace_id="a",
                    session_id="s1",
                    score=0.3,
                    preview="a",
                    column="user_message",
                    method="keyword",
                    rank=2,
                ),
            ],
            semantic_hits=[],
        )
        self.assertEqual([hit.trace_id for hit in fused], ["a", "b"])

    async def test_caps_preview_length(self) -> None:
        fused = await self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    trace_id="t1",
                    session_id="s1",
                    score=0.2,
                    preview="y" * 500,
                    column="user_message",
                    method="keyword",
                    rank=1,
                )
            ],
            semantic_hits=[],
        )
        self.assertEqual(len(fused[0].preview), 200)


class MemorySearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeSearchStore()

    async def test_returns_search_results_with_scores_and_preview(self) -> None:
        self.store.keyword_hits = [
            {"trace_id": "t1", "session_id": "s1", "score": 0.6, "preview": "keyword hit"},
        ]
        self.store.semantic_hits = [
            {"trace_id": "t1", "session_id": "s1", "score": 0.5, "preview": "semantic hit"},
            {"trace_id": "t2", "session_id": "s1", "score": 0.8, "preview": "semantic only"},
        ]
        service = MemorySearchService(  # type: ignore[arg-type]
            self.store,
            retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=True),
        )

        results = await service.search(
            "@user:foo",
            limit=2,
            session_id="s1",
            app_id="app1",
        )

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(
            [result.trace_id for result in results],
            ["t2", "t1"],
        )
        self.assertEqual(results[1].preview, "keyword hit")
        self.assertEqual(results[0].session_id, "s1")
        self.assertEqual(results[1].session_id, "s1")
        self.assertAlmostEqual(results[1].similarity_score, 0.53)

        self.assertEqual(len(self.store.calls), 2)
        for call in self.store.calls:
            self.assertEqual(call["session_id"], "s1")
            self.assertEqual(call["app_id"], "app1")

    async def test_honors_limit_and_supports_keyword_only(self) -> None:
        self.store.keyword_hits = [
            {"trace_id": "t1", "session_id": "s9", "score": 0.9, "preview": "one"},
            {"trace_id": "t2", "session_id": "s9", "score": 0.8, "preview": "two"},
        ]
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        )

        results = await service.search("@agent:test", limit=1, app_id="app1")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].trace_id, "t1")
        self.assertEqual(results[0].session_id, "s9")
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "keyword")
        self.assertEqual(self.store.calls[0]["column"], "agent_response")

    async def test_supports_semantic_only(self) -> None:
        self.store.semantic_hits = [
            {"trace_id": "t9", "session_id": "s3", "score": 0.7, "preview": "sem"},
        ]
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            retrieval_config=RetrievalConfig(enable_keyword=False, enable_semantic=True),
        )

        results = await service.search("@user:test", app_id="app1")

        self.assertEqual([r.trace_id for r in results], ["t9"])
        self.assertEqual([r.session_id for r in results], ["s3"])
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "semantic")

    def test_search_requires_app_id(self) -> None:
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
        )
        with self.assertRaises(TypeError):
            service.search("@user:test")  # type: ignore[call-arg]

    async def test_parse_failure_raises(self) -> None:
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=True),
        )
        with self.assertRaises(ValueError):
            await service.search("hello", app_id="app1")

    async def test_retrieval_failure_raises(self) -> None:
        service = MemorySearchService(FailingKeywordStore())  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError):
            await service.search("@user:boom", app_id="app1")

    async def test_non_positive_limit_returns_nothing(self) -> None:
        service = MemorySearchService(self.store)  # type: ignore[arg-type]
        self.assertEqual(await service.search("@user:x", limit=0, app_id="app1"), [])
        self.assertEqual(self.store.calls, [])


if __name__ == "__main__":
    unittest.main()

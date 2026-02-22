"""Unit tests for phase-1 memory search pipeline."""

from __future__ import annotations

import unittest

from mash.logging.events import MemorySearchEvent
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

    def keyword_search(
        self,
        column: str,
        query_term: str,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "method": "keyword",
                "column": column,
                "query_term": query_term,
                "limit": limit,
                "session_id": session_id,
                "app_id": app_id,
            }
        )
        return self.keyword_hits

    def semantic_search(
        self,
        column: str,
        query_term: str,
        query_embedding: list[float] | None,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
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
            }
        )
        return self.semantic_hits


class FakeEventLogger:
    """In-memory logger test double."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


class FailingKeywordStore(FakeSearchStore):
    """Store that raises during keyword retrieval."""

    def keyword_search(
        self,
        column: str,
        query_term: str,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, object]]:
        raise RuntimeError("keyword retrieval failed")


class QueryParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = QueryParser()

    def test_parses_user_query(self) -> None:
        parsed = self.parser.parse("@user:hello")
        self.assertEqual(parsed.column, "user_message")
        self.assertEqual(parsed.query_term, "hello")
        self.assertIsNone(parsed.semantic_embedding)

    def test_parses_agent_query_and_collapses_whitespace(self) -> None:
        parsed = self.parser.parse("  @agent:  hello   world  ")
        self.assertEqual(parsed.column, "agent_response")
        self.assertEqual(parsed.query_term, "hello world")
        self.assertEqual(parsed.keyword_query, "hello world")
        self.assertEqual(parsed.semantic_query_text, "hello world")

    def test_rejects_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            self.parser.parse("   ")

    def test_rejects_unprefixed_query(self) -> None:
        with self.assertRaises(ValueError):
            self.parser.parse("hello")

    def test_rejects_unknown_prefix(self) -> None:
        with self.assertRaises(ValueError):
            self.parser.parse("@foo:bar")

    def test_rejects_empty_query_term(self) -> None:
        with self.assertRaises(ValueError):
            self.parser.parse("@user:   ")


class RetrievalOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeSearchStore()
        self.parser = QueryParser()
        self.keyword_retriever = KeywordRetriever(self.store)  # type: ignore[arg-type]
        self.semantic_retriever = SemanticRetriever(self.store)  # type: ignore[arg-type]

    def test_keyword_only_calls_only_keyword_search(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.8, "preview": "hello"},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
            config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        )

        parsed = self.parser.parse("@user:hello")
        outputs = orchestrator.retrieve(parsed, limit=5)

        self.assertEqual(len(outputs.keyword_hits), 1)
        self.assertEqual(outputs.keyword_hits[0].turn_id, "t1")
        self.assertEqual(outputs.keyword_hits[0].session_id, "s1")
        self.assertEqual(outputs.keyword_hits[0].preview, "hello")
        self.assertEqual(outputs.semantic_hits, [])
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "keyword")
        self.assertEqual(self.store.calls[0]["column"], "user_message")

    def test_semantic_only_calls_only_semantic_search(self) -> None:
        self.store.semantic_hits = [
            {"turn_id": "t2", "session_id": "s2", "score": 0.9, "preview": "answer"},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
            config=RetrievalConfig(enable_keyword=False, enable_semantic=True),
        )

        parsed = self.parser.parse("@agent:answer")
        outputs = orchestrator.retrieve(parsed, limit=3)

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

    def test_retrieval_caps_preview_length(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.5, "preview": "x" * 300},
        ]
        orchestrator = HybridRetrievalOrchestrator(
            self.keyword_retriever,
            self.semantic_retriever,
        )

        parsed = self.parser.parse("@user:x")
        outputs = orchestrator.retrieve(parsed, limit=2)
        self.assertEqual(len(outputs.keyword_hits[0].preview), 200)


class WeightedFusionRerankerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reranker = WeightedFusionReranker()

    def test_fuses_overlapping_hits_with_weighted_scores(self) -> None:
        keyword_hits = [
            RetrievalHit(
                turn_id="t1",
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
                turn_id="t1",
                session_id="s1",
                score=0.8,
                preview="semantic preview",
                column="user_message",
                method="semantic",
                rank=1,
            )
        ]

        fused = self.reranker.rerank(
            keyword_hits=keyword_hits,
            semantic_hits=semantic_hits,
        )

        self.assertEqual(len(fused), 1)
        self.assertAlmostEqual(fused[0].final_score, 0.71)
        self.assertEqual(fused[0].preview, "keyword preview")
        self.assertEqual(fused[0].session_id, "s1")
        self.assertAlmostEqual(fused[0].semantic_score, 0.8)
        self.assertAlmostEqual(fused[0].keyword_score, 0.5)

    def test_handles_missing_method_scores(self) -> None:
        fused = self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    turn_id="t1",
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
                    turn_id="t2",
                    session_id="s2",
                    score=0.6,
                    preview="s",
                    column="user_message",
                    method="semantic",
                    rank=1,
                )
            ],
        )

        self.assertEqual([hit.turn_id for hit in fused], ["t2", "t1"])
        self.assertAlmostEqual(fused[0].final_score, 0.42)
        self.assertAlmostEqual(fused[1].final_score, 0.12)

    def test_tie_breaks_by_turn_id(self) -> None:
        fused = self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    turn_id="b",
                    session_id="s1",
                    score=0.3,
                    preview="b",
                    column="user_message",
                    method="keyword",
                    rank=1,
                ),
                RetrievalHit(
                    turn_id="a",
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
        self.assertEqual([hit.turn_id for hit in fused], ["a", "b"])

    def test_caps_preview_length(self) -> None:
        fused = self.reranker.rerank(
            keyword_hits=[
                RetrievalHit(
                    turn_id="t1",
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


class MemorySearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeSearchStore()

    def test_returns_search_results_with_scores_and_preview(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.6, "preview": "keyword hit"},
        ]
        self.store.semantic_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.5, "preview": "semantic hit"},
            {"turn_id": "t2", "session_id": "s1", "score": 0.8, "preview": "semantic only"},
        ]
        service = MemorySearchService(self.store)  # type: ignore[arg-type]

        results = service.search(
            "@user:foo",
            limit=2,
            session_id="s1",
            app_id="app1",
        )

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(
            [result.turn_id for result in results],
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

    def test_honors_limit_and_supports_keyword_only(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s9", "score": 0.9, "preview": "one"},
            {"turn_id": "t2", "session_id": "s9", "score": 0.8, "preview": "two"},
        ]
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        )

        results = service.search("@agent:test", limit=1, app_id="app1")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].turn_id, "t1")
        self.assertEqual(results[0].session_id, "s9")
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "keyword")
        self.assertEqual(self.store.calls[0]["column"], "agent_response")

    def test_supports_semantic_only(self) -> None:
        self.store.semantic_hits = [
            {"turn_id": "t9", "session_id": "s3", "score": 0.7, "preview": "sem"},
        ]
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            retrieval_config=RetrievalConfig(enable_keyword=False, enable_semantic=True),
        )

        results = service.search("@user:test", app_id="app1")

        self.assertEqual([r.turn_id for r in results], ["t9"])
        self.assertEqual([r.session_id for r in results], ["s3"])
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.store.calls[0]["method"], "semantic")

    def test_search_requires_app_id(self) -> None:
        service = MemorySearchService(self.store)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            service.search("@user:test")  # type: ignore[call-arg]

    def test_emits_per_stage_memory_search_events_on_success(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.6, "preview": "keyword hit"},
        ]
        self.store.semantic_hits = [
            {"turn_id": "t2", "session_id": "s1", "score": 0.8, "preview": "semantic hit"},
        ]
        event_logger = FakeEventLogger()
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            event_logger=event_logger,  # type: ignore[arg-type]
        )

        results = service.search("@user:hello world", app_id="app1", session_id="s1")

        self.assertEqual(len(results), 2)
        self.assertEqual(
            [getattr(event, "event_type") for event in event_logger.events],
            [
                "memory.search.parse.complete",
                "memory.search.start",
                "memory.search.retrieval.complete",
                "memory.search.rerank.complete",
                "memory.search.complete",
            ],
        )
        self.assertTrue(
            all(isinstance(event, MemorySearchEvent) for event in event_logger.events)
        )
        query_ids = {getattr(event, "query_id") for event in event_logger.events}
        self.assertEqual(len(query_ids), 1)
        query_id = next(iter(query_ids))
        self.assertIsInstance(query_id, str)
        self.assertTrue(query_id)

        parse_event = event_logger.events[0]
        self.assertEqual(getattr(parse_event, "stage"), "parse")
        self.assertEqual(getattr(parse_event, "level"), "INFO")
        self.assertEqual(getattr(parse_event, "metadata")["query_term"], "hello world")

        start_event = event_logger.events[1]
        start_metadata = getattr(start_event, "metadata")
        self.assertEqual(start_metadata["query_term"], "hello world")
        self.assertEqual(start_metadata["column"], "user_message")
        self.assertEqual(start_metadata["limit_requested"], 10)
        self.assertEqual(start_metadata["limit_normalized"], 10)
        self.assertTrue(start_metadata["has_session_id"])
        self.assertNotIn("query_length", start_metadata)
        self.assertEqual(start_event.to_dict()["event_class"], "MemorySearchEvent")

    def test_parse_failure_emits_error_event_without_start(self) -> None:
        event_logger = FakeEventLogger()
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            event_logger=event_logger,  # type: ignore[arg-type]
        )

        with self.assertRaises(ValueError):
            service.search("hello", app_id="app1")

        self.assertEqual(len(event_logger.events), 1)
        error_event = event_logger.events[0]
        self.assertEqual(getattr(error_event, "event_type"), "memory.search.parse.error")
        self.assertEqual(getattr(error_event, "stage"), "parse")
        self.assertEqual(getattr(error_event, "level"), "ERROR")
        self.assertIn("must start", getattr(error_event, "error"))

    def test_retrieval_failure_emits_error_event(self) -> None:
        store = FailingKeywordStore()
        event_logger = FakeEventLogger()
        service = MemorySearchService(
            store,  # type: ignore[arg-type]
            event_logger=event_logger,  # type: ignore[arg-type]
        )

        with self.assertRaises(RuntimeError):
            service.search("@user:boom", app_id="app1")

        self.assertEqual(
            [getattr(event, "event_type") for event in event_logger.events],
            [
                "memory.search.parse.complete",
                "memory.search.start",
                "memory.search.retrieval.error",
            ],
        )
        error_event = event_logger.events[-1]
        self.assertEqual(getattr(error_event, "stage"), "retrieval")
        self.assertEqual(getattr(error_event, "error"), "keyword retrieval failed")

    def test_rerank_failure_emits_error_event(self) -> None:
        self.store.keyword_hits = [
            {"turn_id": "t1", "session_id": "s1", "score": 0.5, "preview": "hit"},
        ]
        self.store.semantic_hits = [
            {"turn_id": "t1", "session_id": "s2", "score": 0.6, "preview": "other"},
        ]
        event_logger = FakeEventLogger()
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            event_logger=event_logger,  # type: ignore[arg-type]
        )

        with self.assertRaises(ValueError):
            service.search("@user:boom", app_id="app1")

        self.assertEqual(
            [getattr(event, "event_type") for event in event_logger.events],
            [
                "memory.search.parse.complete",
                "memory.search.start",
                "memory.search.retrieval.complete",
                "memory.search.rerank.error",
            ],
        )
        error_event = event_logger.events[-1]
        self.assertEqual(getattr(error_event, "stage"), "rerank")
        self.assertIn("Conflicting session_id", getattr(error_event, "error"))

    def test_non_positive_limit_short_circuit_emits_complete_event(self) -> None:
        event_logger = FakeEventLogger()
        service = MemorySearchService(
            self.store,  # type: ignore[arg-type]
            event_logger=event_logger,  # type: ignore[arg-type]
        )

        results = service.search("@user:hello", app_id="app1", limit=0)

        self.assertEqual(results, [])
        self.assertEqual(len(self.store.calls), 0)
        self.assertEqual(len(event_logger.events), 1)
        complete_event = event_logger.events[0]
        self.assertEqual(getattr(complete_event, "event_type"), "memory.search.complete")
        self.assertEqual(getattr(complete_event, "stage"), "service")
        self.assertEqual(getattr(complete_event, "level"), "INFO")
        self.assertEqual(
            getattr(complete_event, "metadata"),
            {
                "result_count": 0,
                "limit_applied": 0,
                "short_circuit": "non_positive_limit",
            },
        )


if __name__ == "__main__":
    unittest.main()

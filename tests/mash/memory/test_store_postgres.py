"""Regression tests for the Postgres-backed memory store."""

from __future__ import annotations

import math
import os
import unittest
import uuid
from typing import Any

from mash.logging import AgentTraceEvent
from mash.logging.events import normalize_log_event
from mash.memory.store import PostgresStore

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover - exercised when optional deps are missing
    psycopg = None


def _memory_database_url() -> str:
    return os.environ.get(
        "MASH_REAL_MEMORY_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash_memory",
    )


def _require_postgres_memory_support() -> str:
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    database_url = _memory_database_url()
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres memory store unavailable: {exc}") from exc
    return database_url


def _delete_app_rows(database_url: str, app_id: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM memory_logs WHERE app_id = %s", (app_id,))
            cursor.execute("DELETE FROM memory_turns WHERE app_id = %s", (app_id,))


class PostgresStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = _require_postgres_memory_support()
        self.store = PostgresStore(self.database_url)
        await self.store.open()
        self.app_id = f"pg-memory-{uuid.uuid4().hex}"
        self.other_app_id = f"{self.app_id}-other"
        _delete_app_rows(self.database_url, self.app_id)
        _delete_app_rows(self.database_url, self.other_app_id)
        self._turn_counter = 0

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_app_rows(self.database_url, self.app_id)
        _delete_app_rows(self.database_url, self.other_app_id)

    async def _save_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        agent_response: str,
        app_id: str | None = None,
        signals: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        session_total_tokens: int = 0,
    ) -> str:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        await self.store.save_turn(
            trace_id=turn_id,
            session_id=session_id,
            app_id=app_id or self.app_id,
            user_message=user_message,
            agent_response=agent_response,
            signals=signals or {},
            session_total_tokens=session_total_tokens,
            metadata=metadata,
        )
        return turn_id

    async def test_save_and_get_logs_reconstruct_public_event_shape(self) -> None:
        await self.store.save_logs(
            [
                normalize_log_event(
                    AgentTraceEvent(
                        event_type="agent.run.start",
                        app_id=self.app_id,
                        session_id="session-1",
                        trace_id="trace-1",
                        payload={"user_message": "hello"},
                        ts=123.0,
                    )
                )
            ]
        )

        events = await self.store.get_logs(app_id=self.app_id, session_id="session-1")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_type"], "agent.run.start")
        self.assertEqual(event["ts"], 123.0)
        self.assertEqual(event["trace_id"], "trace-1")
        self.assertEqual(event["payload"], {"user_message": "hello"})
        self.assertIn("step_id", event)
        self.assertIsNone(event["step_id"])
        self.assertIn("duration_ms", event)
        self.assertIsNone(event["duration_ms"])
        self.assertIn("log_id", event)

    async def test_latest_and_recent_log_traces_use_log_table(self) -> None:
        await self.store.save_logs(
            [
                {
                    "app_id": self.app_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": self.app_id,
                    "session_id": "session-1",
                    "trace_id": "trace-2",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": self.app_id,
                    "session_id": "session-1",
                    "trace_id": "trace-2",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.complete",
                    "created_at": 3.0,
                    "payload": {"payload": {}},
                },
            ]
        )

        latest = await self.store.get_latest_log_trace(
            app_id=self.app_id,
            session_id="session-1",
        )
        recent = await self.store.list_recent_log_traces(
            app_id=self.app_id,
            session_id="session-1",
            limit=2,
        )

        assert latest is not None
        self.assertEqual(latest["trace_id"], "trace-2")
        self.assertEqual(latest["event_count"], 2)
        self.assertEqual([item["trace_id"] for item in recent], ["trace-2", "trace-1"])

    async def test_get_logs_supports_cursoring_with_after_log_id(self) -> None:
        await self.store.save_logs(
            [
                {
                    "app_id": self.app_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": self.app_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.complete",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
            ]
        )

        initial = await self.store.get_logs(app_id=self.app_id)
        assert initial
        tail = await self.store.get_logs(
            app_id=self.app_id,
            after_log_id=int(initial[0]["log_id"]),
        )

        self.assertEqual([item["event_type"] for item in tail], ["agent.run.complete"])

    async def test_save_turn_and_get_turns_preserve_signals_metadata_and_order(self) -> None:
        turn_1 = await self._save_turn(
            session_id="session-a",
            user_message="first user",
            agent_response="first agent",
            signals={"unused_tool_tokens": 42, "unused_tools": ["alpha", "beta"]},
            metadata={"trace_id": "trace-1"},
            session_total_tokens=9,
        )
        turn_2 = await self._save_turn(
            session_id="session-a",
            user_message="second user",
            agent_response="second agent",
            metadata={"trace_id": "trace-2"},
            session_total_tokens=11,
        )

        turns = await self.store.get_turns(
            session_id="session-a",
            app_id=self.app_id,
            limit=None,
        )
        limited = await self.store.get_turns(
            session_id="session-a",
            app_id=self.app_id,
            limit=1,
        )

        self.assertEqual([turn["turn_id"] for turn in turns], [turn_1, turn_2])
        self.assertEqual([turn["turn_id"] for turn in limited], [turn_2])
        self.assertEqual(turns[0]["signals"]["unused_tool_tokens"], 42)
        self.assertEqual(turns[0]["signals"]["unused_tools"], ["alpha", "beta"])
        self.assertEqual(turns[0]["metadata"], {"trace_id": "trace-1"})
        self.assertEqual(turns[1]["session_total_tokens"], 11)

    async def test_get_turns_honors_optional_app_id_filter(self) -> None:
        await self._save_turn(
            session_id="shared-session",
            user_message="from app a",
            agent_response="a",
            app_id=self.app_id,
        )
        await self._save_turn(
            session_id="shared-session",
            user_message="from app b",
            agent_response="b",
            app_id=self.other_app_id,
        )

        filtered = await self.store.get_turns(
            session_id="shared-session",
            app_id=self.app_id,
            limit=None,
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["user_message"], "from app a")
        other = await self.store.get_turns(
            session_id="shared-session",
            app_id=self.other_app_id,
            limit=None,
        )
        self.assertEqual(len(other), 1)

    async def test_list_sessions_and_latest_session_are_app_scoped(self) -> None:
        await self._save_turn(
            session_id="session-a",
            user_message="first",
            agent_response="one",
            session_total_tokens=3,
        )
        await self._save_turn(
            session_id="session-b",
            user_message="second",
            agent_response="two",
            session_total_tokens=7,
        )
        await self._save_turn(
            session_id="session-x",
            user_message="other app",
            agent_response="other response",
            app_id=self.other_app_id,
            session_total_tokens=99,
        )

        sessions = await self.store.list_sessions(app_id=self.app_id)
        latest = await self.store.get_latest_session(app_id=self.app_id)

        self.assertEqual([item["session_id"] for item in sessions], ["session-b", "session-a"])
        assert latest is not None
        self.assertEqual(latest["session_id"], "session-b")
        self.assertEqual(latest["session_total_tokens"], 7)

    async def test_recent_and_latest_trace_lookup_are_newest_first(self) -> None:
        await self._save_turn(
            session_id="session-a",
            user_message="first",
            agent_response="one",
            metadata={"trace_id": "turn-1"},
        )
        second = await self._save_turn(
            session_id="session-a",
            user_message="second",
            agent_response="two",
            metadata={"trace_id": "turn-2"},
        )

        recent = await self.store.list_recent_traces(
            app_id=self.app_id,
            session_id="session-a",
            limit=2,
        )
        latest = await self.store.get_latest_trace(
            app_id=self.app_id,
            session_id="session-a",
        )

        self.assertEqual([item["trace_id"] for item in recent], [second, "turn-1"])
        assert latest is not None
        self.assertEqual(latest["trace_id"], second)

    async def test_get_turn_by_ids_preserves_request_order_and_omits_missing(self) -> None:
        turn_1 = await self._save_turn(
            session_id="s1",
            user_message="first user",
            agent_response="first agent",
        )
        turn_2 = await self._save_turn(
            session_id="s2",
            user_message="second user",
            agent_response="second agent",
        )

        turns = await self.store.get_turn_by_ids(
            [
                {"session_id": "s2", "turn_id": turn_2},
                {"session_id": "s1", "turn_id": "missing"},
                {"session_id": "s1", "turn_id": turn_1},
            ],
            app_id=self.app_id,
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(
            [(turn["session_id"], turn["turn_id"]) for turn in turns],
            [("s2", turn_2), ("s1", turn_1)],
        )

    async def test_get_turn_by_ids_honors_optional_app_id_filter(self) -> None:
        turn_a = await self._save_turn(
            session_id="shared-session",
            user_message="from app a",
            agent_response="a",
            app_id=self.app_id,
        )
        turn_b = await self._save_turn(
            session_id="shared-session",
            user_message="from app b",
            agent_response="b",
            app_id=self.other_app_id,
        )

        filtered = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "turn_id": turn_b}],
            app_id=self.app_id,
        )
        matching_a = await self.store.get_turn_by_ids(
            [
                {"session_id": "shared-session", "turn_id": turn_b},
                {"session_id": "shared-session", "turn_id": turn_a},
            ],
            app_id=self.app_id,
        )
        matching_b = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "turn_id": turn_b}],
            app_id=self.other_app_id,
        )

        self.assertIsNone(filtered)
        self.assertIsNotNone(matching_a)
        assert matching_a is not None
        self.assertEqual([turn["turn_id"] for turn in matching_a], [turn_a])
        self.assertIsNotNone(matching_b)
        assert matching_b is not None
        self.assertEqual([turn["turn_id"] for turn in matching_b], [turn_b])

    async def test_keyword_search_returns_empty_for_blank_query_or_non_positive_limit(
        self,
    ) -> None:
        await self._save_turn(
            session_id="s1",
            user_message="hello world",
            agent_response="response",
        )

        self.assertEqual(
            await self.store.keyword_search("user_message", "hello", limit=0),
            [],
        )
        self.assertEqual(
            await self.store.keyword_search("user_message", "hello", limit=-1),
            [],
        )
        self.assertEqual(
            await self.store.keyword_search("user_message", "   ", limit=5),
            [],
        )

    async def test_keyword_search_is_column_scoped_and_filtered(self) -> None:
        user_hit = await self._save_turn(
            session_id="s1",
            app_id=self.app_id,
            user_message="hello world",
            agent_response="irrelevant response",
        )
        await self._save_turn(
            session_id="s1",
            app_id=self.app_id,
            user_message="hello only",
            agent_response="world hello",
        )
        agent_hit = await self._save_turn(
            session_id="s1",
            app_id=self.app_id,
            user_message="no match here",
            agent_response="hello world",
        )
        await self._save_turn(
            session_id="s2",
            app_id=self.other_app_id,
            user_message="hello world",
            agent_response="hello world",
        )

        user_results = await self.store.keyword_search(
            "user_message",
            "hello world",
            limit=10,
            session_id="s1",
            app_id=self.app_id,
        )
        agent_results = await self.store.keyword_search(
            "agent_response",
            "hello world",
            limit=10,
            session_id="s1",
            app_id=self.app_id,
        )

        self.assertEqual([hit["turn_id"] for hit in user_results], [user_hit])
        self.assertEqual(user_results[0]["preview"], "hello world")
        self.assertEqual(
            {hit["turn_id"] for hit in agent_results},
            {agent_hit, "turn-2"},
        )
        self.assertNotIn(user_hit, {hit["turn_id"] for hit in agent_results})

    async def test_keyword_search_uses_rank_based_score_normalization(self) -> None:
        await self._save_turn(
            session_id="s1",
            user_message="alpha",
            agent_response="x",
        )
        await self._save_turn(
            session_id="s1",
            user_message="alpha beta",
            agent_response="x",
        )
        await self._save_turn(
            session_id="s1",
            user_message="beta alpha gamma",
            agent_response="x",
        )

        results = await self.store.keyword_search("user_message", "alpha", limit=3)

        self.assertEqual(len(results), 3)
        self.assertTrue(math.isclose(float(results[0]["score"]), 0.5))
        self.assertTrue(math.isclose(float(results[1]["score"]), 1.0 / 3.0))
        self.assertTrue(math.isclose(float(results[2]["score"]), 0.25))

    async def test_semantic_search_is_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            await self.store.semantic_search(
                "user_message",
                "hello",
                query_embedding=None,
                limit=5,
            )


if __name__ == "__main__":
    unittest.main()

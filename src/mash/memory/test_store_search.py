"""Tests for SQLiteStore keyword search."""

from __future__ import annotations

import time
import unittest

from mash.memory.store import SQLiteStore


class SQLiteStoreKeywordSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteStore(":memory:")
        self._turn_counter = 0

    def _save_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        agent_response: str,
        app_id: str,
    ) -> str:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        self.store.save_turn(
            trace_id=turn_id,
            session_id=session_id,
            app_id=app_id,
            user_message=user_message,
            agent_response=agent_response,
            signals={},
            session_total_tokens=0,
        )
        return turn_id

    def _insert_turn_with_app(
        self,
        *,
        turn_id: str,
        session_id: str,
        app_id: str,
        user_message: str,
        agent_response: str,
    ) -> None:
        now = time.time()
        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO turns (
                    turn_id,
                    session_id,
                    app_id,
                    user_message,
                    agent_response,
                    session_total_tokens,
                    metadata,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    app_id,
                    user_message,
                    agent_response,
                    0,
                    "{}",
                    now,
                ),
            )
            self.store._conn.execute(
                """
                INSERT INTO fts_turns (turn_id, session_id, user_message, agent_response)
                VALUES (?, ?, ?, ?)
                """,
                (turn_id, session_id, user_message, agent_response),
            )
            self.store._conn.commit()

    def test_keyword_search_returns_empty_for_non_positive_limit_or_blank_query(
        self,
    ) -> None:
        self._save_turn(
            session_id="s1",
            app_id="app",
            user_message="hello world",
            agent_response="response",
        )

        self.assertEqual(
            self.store.keyword_search("user_message", "hello", limit=0),
            [],
        )
        self.assertEqual(
            self.store.keyword_search("user_message", "hello", limit=-1),
            [],
        )
        self.assertEqual(
            self.store.keyword_search("user_message", "   ", limit=5),
            [],
        )

    def test_keyword_search_is_column_scoped_and_token_and(self) -> None:
        user_hit = self._save_turn(
            session_id="s1",
            app_id="app",
            user_message="hello world",
            agent_response="irrelevant response",
        )
        self._save_turn(
            session_id="s1",
            app_id="app",
            user_message="hello only",
            agent_response="world hello",
        )
        agent_hit = self._save_turn(
            session_id="s1",
            app_id="app",
            user_message="no match here",
            agent_response="hello world",
        )

        user_results = self.store.keyword_search(
            "user_message", "hello world", limit=10
        )
        self.assertEqual([hit["turn_id"] for hit in user_results], [user_hit])
        self.assertEqual(user_results[0]["preview"], "hello world")
        self.assertEqual(user_results[0]["session_id"], "s1")

        agent_results = self.store.keyword_search(
            "agent_response",
            "hello world",
            limit=10,
        )
        self.assertEqual(
            {hit["turn_id"] for hit in agent_results}, {agent_hit, "turn-2"}
        )
        self.assertNotIn(user_hit, {hit["turn_id"] for hit in agent_results})

    def test_keyword_search_uses_rank_based_score_normalization(self) -> None:
        self._save_turn(
            session_id="s1", app_id="app", user_message="alpha", agent_response="x"
        )
        self._save_turn(
            session_id="s1", app_id="app", user_message="alpha beta", agent_response="x"
        )
        self._save_turn(
            session_id="s1",
            app_id="app",
            user_message="beta alpha gamma",
            agent_response="x",
        )

        results = self.store.keyword_search("user_message", "alpha", limit=3)

        self.assertEqual(len(results), 3)
        self.assertAlmostEqual(float(results[0]["score"]), 0.5)
        self.assertAlmostEqual(float(results[1]["score"]), 1.0 / 3.0)
        self.assertAlmostEqual(float(results[2]["score"]), 0.25)
        for hit in results:
            self.assertGreaterEqual(float(hit["score"]), 0.0)
            self.assertLessEqual(float(hit["score"]), 1.0)

    def test_keyword_search_filters_by_session_id(self) -> None:
        wanted = self._save_turn(
            session_id="session-a",
            app_id="app",
            user_message="shared keyword",
            agent_response="x",
        )
        self._save_turn(
            session_id="session-b",
            app_id="app",
            user_message="shared keyword",
            agent_response="x",
        )

        results = self.store.keyword_search(
            "user_message",
            "shared keyword",
            limit=10,
            session_id="session-a",
        )

        self.assertEqual([hit["turn_id"] for hit in results], [wanted])
        self.assertEqual(results[0]["session_id"], "session-a")

    def test_keyword_search_filters_by_app_id(self) -> None:
        self._insert_turn_with_app(
            turn_id="app-a-turn",
            session_id="s1",
            app_id="app-a",
            user_message="cross app keyword",
            agent_response="x",
        )
        self._insert_turn_with_app(
            turn_id="app-b-turn",
            session_id="s1",
            app_id="app-b",
            user_message="cross app keyword",
            agent_response="x",
        )

        results = self.store.keyword_search(
            "user_message",
            "cross app keyword",
            limit=10,
            app_id="app-a",
        )

        self.assertEqual([hit["turn_id"] for hit in results], ["app-a-turn"])

    def test_keyword_search_filters_by_app_id_for_turns_saved_via_save_turn(
        self,
    ) -> None:
        wanted = self._save_turn(
            session_id="s1",
            app_id="app-a",
            user_message="saved via save_turn",
            agent_response="x",
        )
        self._save_turn(
            session_id="s1",
            app_id="app-b",
            user_message="saved via save_turn",
            agent_response="x",
        )

        results = self.store.keyword_search(
            "user_message",
            "saved via save_turn",
            limit=10,
            app_id="app-a",
        )

        self.assertEqual([hit["turn_id"] for hit in results], [wanted])

    def test_keyword_search_combines_session_and_app_filters(self) -> None:
        self._insert_turn_with_app(
            turn_id="match",
            session_id="s1",
            app_id="app-a",
            user_message="combo filter token",
            agent_response="x",
        )
        self._insert_turn_with_app(
            turn_id="wrong-session",
            session_id="s2",
            app_id="app-a",
            user_message="combo filter token",
            agent_response="x",
        )
        self._insert_turn_with_app(
            turn_id="wrong-app",
            session_id="s1",
            app_id="app-b",
            user_message="combo filter token",
            agent_response="x",
        )

        results = self.store.keyword_search(
            "user_message",
            "combo filter token",
            limit=10,
            session_id="s1",
            app_id="app-a",
        )

        self.assertEqual([hit["turn_id"] for hit in results], ["match"])


if __name__ == "__main__":
    unittest.main()

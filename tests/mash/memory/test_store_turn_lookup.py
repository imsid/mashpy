"""Tests for SQLiteStore batched turn lookup."""

from __future__ import annotations

import unittest

from mash.memory.store import SQLiteStore


class SQLiteStoreTurnLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = SQLiteStore(":memory:")
        self._turn_counter = 0

    async def _save_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        agent_response: str,
        app_id: str = "test-app",
    ) -> str:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        await self.store.save_turn(
            trace_id=turn_id,
            session_id=session_id,
            app_id=app_id,
            user_message=user_message,
            agent_response=agent_response,
            signals={},
            session_total_tokens=0,
        )
        return turn_id

    async def test_get_turn_by_ids_returns_full_turn_for_exact_pair(self) -> None:
        turn_id = await self._save_turn(
            session_id="s1",
            user_message="hello user",
            agent_response="hello agent",
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s1", "turn_id": turn_id}],
            app_id="test-app",
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], turn_id)
        self.assertEqual(turns[0]["session_id"], "s1")
        self.assertEqual(turns[0]["user_message"], "hello user")
        self.assertEqual(turns[0]["agent_response"], "hello agent")

    async def test_get_turn_by_ids_returns_none_for_wrong_session(self) -> None:
        turn_id = await self._save_turn(
            session_id="s1",
            user_message="hello user",
            agent_response="hello agent",
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s2", "turn_id": turn_id}],
            app_id="test-app",
        )

        self.assertIsNone(turns)

    async def test_get_turn_by_ids_returns_none_for_unknown_turn(self) -> None:
        await self._save_turn(
            session_id="s1",
            user_message="hello user",
            agent_response="hello agent",
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s1", "turn_id": "unknown-turn"}],
            app_id="test-app",
        )

        self.assertIsNone(turns)

    async def test_get_turn_by_ids_preserves_exact_text_content(self) -> None:
        turn_id = await self._save_turn(
            session_id="s1",
            user_message="line1\nline2\tuser",
            agent_response='lineA\nlineB "quoted"',
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s1", "turn_id": turn_id}],
            app_id="test-app",
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(turns[0]["user_message"], "line1\nline2\tuser")
        self.assertEqual(turns[0]["agent_response"], 'lineA\nlineB "quoted"')

    async def test_get_turn_by_ids_returns_multiple_turns_in_request_order(
        self,
    ) -> None:
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
                {"session_id": "s1", "turn_id": turn_1},
            ],
            app_id="test-app",
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(
            [(turn["session_id"], turn["turn_id"]) for turn in turns],
            [("s2", turn_2), ("s1", turn_1)],
        )

    async def test_get_turn_by_ids_omits_missing_pairs_and_returns_found_matches(
        self,
    ) -> None:
        turn_1 = await self._save_turn(
            session_id="s1",
            user_message="first user",
            agent_response="first agent",
        )

        turns = await self.store.get_turn_by_ids(
            [
                {"session_id": "s1", "turn_id": turn_1},
                {"session_id": "s1", "turn_id": "missing"},
            ],
            app_id="test-app",
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], turn_1)

    async def test_get_turn_by_ids_honors_optional_app_id_filter(self) -> None:
        turn_a = await self._save_turn(
            session_id="shared-session",
            user_message="from app a",
            agent_response="a",
            app_id="app-a",
        )
        turn_b = await self._save_turn(
            session_id="shared-session",
            app_id="app-b",
            user_message="from app b",
            agent_response="b",
        )

        filtered = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "turn_id": turn_b}],
            app_id="app-a",
        )
        matching_a = await self.store.get_turn_by_ids(
            [
                {"session_id": "shared-session", "turn_id": turn_b},
                {"session_id": "shared-session", "turn_id": turn_a},
            ],
            app_id="app-a",
        )
        matching_b = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "turn_id": turn_b}],
            app_id="app-b",
        )

        self.assertIsNone(filtered)
        self.assertIsNotNone(matching_a)
        assert matching_a is not None
        self.assertEqual([turn["turn_id"] for turn in matching_a], [turn_a])
        self.assertIsNotNone(matching_b)
        assert matching_b is not None
        self.assertEqual([turn["turn_id"] for turn in matching_b], [turn_b])

    async def test_list_workflow_turns_filters_by_app_and_session_prefix(self) -> None:
        run_session = (
            "workflow:masher-trace-digest:task:digest-traces:run:"
            "mw:h_TI1UUyBX5w8Q:masher-trace-digest:bHfMwMfMsPDPHI60"
        )
        turn_id = await self._save_turn(
            session_id=run_session,
            app_id="masher",
            user_message="digest input",
            agent_response="digest output",
        )
        await self._save_turn(
            session_id="workflow:other:task:digest-traces:run:mw:h_1:other:r1",
            app_id="masher",
            user_message="other workflow",
            agent_response="ignore",
        )
        await self._save_turn(
            session_id=run_session,
            app_id="other-app",
            user_message="other app",
            agent_response="ignore",
        )

        turns = await self.store.list_workflow_turns(
            app_id="masher",
            session_prefix="workflow:masher-trace-digest:task:digest-traces:run:",
            limit=10,
            sort_desc=False,
        )

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], turn_id)
        self.assertEqual(turns[0]["session_id"], run_session)
        self.assertEqual(turns[0]["user_message"], "digest input")
        self.assertEqual(turns[0]["agent_response"], "digest output")


if __name__ == "__main__":
    unittest.main()

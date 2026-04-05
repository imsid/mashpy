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
            [{"session_id": "s1", "turn_id": turn_id}]
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
            [{"session_id": "s2", "turn_id": turn_id}]
        )

        self.assertIsNone(turns)

    async def test_get_turn_by_ids_returns_none_for_unknown_turn(self) -> None:
        await self._save_turn(
            session_id="s1",
            user_message="hello user",
            agent_response="hello agent",
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s1", "turn_id": "unknown-turn"}]
        )

        self.assertIsNone(turns)

    async def test_get_turn_by_ids_preserves_exact_text_content(self) -> None:
        turn_id = await self._save_turn(
            session_id="s1",
            user_message="line1\nline2\tuser",
            agent_response='lineA\nlineB "quoted"',
        )

        turns = await self.store.get_turn_by_ids(
            [{"session_id": "s1", "turn_id": turn_id}]
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
            ]
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
            ]
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], turn_1)


if __name__ == "__main__":
    unittest.main()

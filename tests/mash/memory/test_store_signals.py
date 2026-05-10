"""Tests for SQLite signal storage."""

from __future__ import annotations

import json
import unittest

from mash.memory.store import SQLiteStore


class SQLiteStoreSignalsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_db_creates_json_backed_signal_rows(self) -> None:
        store = SQLiteStore(":memory:")
        await store.save_turn(
            trace_id="turn-1",
            session_id="session-1",
            app_id="app-1",
            user_message="hello",
            agent_response="world",
            signals={"unused_tool_tokens": 42, "unused_tools": ["alpha", "beta"]},
            session_total_tokens=0,
            metadata={},
        )

        assert store._conn is not None
        async with store._lock:
            columns_cursor = await store._conn.execute("PRAGMA table_info(signals)")
            columns = await columns_cursor.fetchall()
            column_names = {str(row[1]) for row in columns}
            rows_cursor = await store._conn.execute(
                """
                SELECT turn_id, session_id, app_id, signal_name, signal_value
                FROM signals
                WHERE turn_id = ?
                ORDER BY signal_name ASC
                """,
                ("turn-1",),
            )
            rows = [tuple(row) for row in await rows_cursor.fetchall()]

        self.assertTrue({"turn_id", "session_id", "app_id", "signal_name", "signal_value"}.issubset(column_names))
        self.assertCountEqual(
            rows,
            [
                (
                    "turn-1",
                    "session-1",
                    "app-1",
                    "unused_tools",
                    json.dumps(["alpha", "beta"]),
                ),
                (
                    "turn-1",
                    "session-1",
                    "app-1",
                    "unused_tool_tokens",
                    json.dumps(42),
                ),
            ],
        )
        turns = await store.get_turns(
            session_id="session-1",
            app_id="app-1",
            limit=1,
        )
        self.assertEqual(turns[0]["signals"]["unused_tool_tokens"], 42)
        self.assertEqual(turns[0]["signals"]["unused_tools"], ["alpha", "beta"])

    async def test_get_turns_honors_optional_app_id_filter(self) -> None:
        store = SQLiteStore(":memory:")
        await store.save_turn(
            trace_id="turn-app-a",
            session_id="session-1",
            app_id="app-a",
            user_message="hello from a",
            agent_response="world a",
            signals={},
            session_total_tokens=0,
            metadata={},
        )
        await store.save_turn(
            trace_id="turn-app-b",
            session_id="session-1",
            app_id="app-b",
            user_message="hello from b",
            agent_response="world b",
            signals={},
            session_total_tokens=0,
            metadata={},
        )

        filtered = await store.get_turns(
            session_id="session-1",
            app_id="app-a",
            limit=None,
        )
        self.assertEqual([turn["turn_id"] for turn in filtered], ["turn-app-a"])
        other = await store.get_turns(
            session_id="session-1",
            app_id="app-b",
            limit=None,
        )
        self.assertEqual([turn["turn_id"] for turn in other], ["turn-app-b"])

if __name__ == "__main__":
    unittest.main()

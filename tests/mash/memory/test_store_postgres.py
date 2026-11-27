"""Regression tests for the Postgres-backed memory store."""

from __future__ import annotations

import math
import os
import unittest
import uuid
from pathlib import Path
from typing import Any

from mash.logging import CommandEvent
from mash.logging.events import normalize_log_event
from mash.memory.store import PostgresStore

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover - exercised when optional deps are missing
    psycopg = None


def _memory_database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash",
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


def _delete_app_rows(database_url: str, agent_id: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM memory_logs WHERE agent_id = %s", (agent_id,))
            cursor.execute("DELETE FROM memory_turns WHERE agent_id = %s", (agent_id,))
            cursor.execute("DELETE FROM memory_session WHERE agent_id = %s", (agent_id,))


class PostgresStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = _require_postgres_memory_support()
        self.store = PostgresStore(self.database_url)
        await self.store.open()
        self.agent_id = f"pg-memory-{uuid.uuid4().hex}"
        self.other_agent_id = f"{self.agent_id}-other"
        _delete_app_rows(self.database_url, self.agent_id)
        _delete_app_rows(self.database_url, self.other_agent_id)
        self._turn_counter = 0

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_app_rows(self.database_url, self.agent_id)
        _delete_app_rows(self.database_url, self.other_agent_id)

    async def _save_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        agent_response: str,
        agent_id: str | None = None,
        signals: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        context_tokens: int = 0,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        app_id: str | None = None,
    ) -> str:
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        await self.store.append_entry(
            trace_id=turn_id,
            session_id=session_id,
            agent_id=agent_id or self.agent_id,
            user_message=user_message,
            agent_response=agent_response,
            signals=signals or {},
            context_tokens=context_tokens,
            metadata=metadata,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            replayable=workflow_id is None,
            app_id=app_id,
        )
        return turn_id

    async def _replay_fixture(self, session_id: str, spec: list[tuple[str, bool, bool]]) -> None:
        """Save turns (name, replayable, summary) at created_at 1, 2, 3, ..."""
        for index, (name, replayable, summary) in enumerate(spec, start=1):
            trace_id = f"{self.agent_id}-{name}"
            await self.store.append_entry(
                trace_id=trace_id,
                session_id=session_id,
                agent_id=self.agent_id,
                user_message=f"u-{name}",
                agent_response=f"a-{name}",
                signals={"s": 1},
                context_tokens=0,
                metadata={"type": "summary_checkpoint"} if summary else None,
                replayable=replayable,
            )
            with psycopg.connect(self.database_url, autocommit=True) as conn:
                conn.execute(
                    "UPDATE memory_turns SET created_at = %s WHERE trace_id = %s",
                    (float(index), trace_id),
                )

    def _sid(self, name: str) -> str:
        return f"{name}-{self.agent_id}"

    def _names(self, turns: list[dict[str, Any]]) -> list[str]:
        return [turn["trace_id"].removeprefix(f"{self.agent_id}-") for turn in turns]

    async def test_replay_turns_without_a_summary_are_every_replayable_turn(self) -> None:
        await self._replay_fixture(
            self._sid("replay-1"),
            [("t1", True, False), ("t2", True, False), ("wf", False, False), ("t3", True, False)],
        )

        turns = await self.store.get_replay_turns(self._sid("replay-1"))

        self.assertEqual(self._names(turns), ["t1", "t2", "t3"])
        self.assertNotIn("signals", turns[0])
        self.assertEqual(turns[0]["user_message"], "u-t1")
        self.assertEqual(await self.store.get_replay_turns(self._sid("replay-none")), [])

    async def test_replay_turns_by_trace_id_are_those_turns_in_order(self) -> None:
        await self._replay_fixture(
            self._sid("replay-3"),
            [("t1", True, False), ("t2", True, False), ("t3", True, False)],
        )

        turns = await self.store.get_replay_turns(
            self._sid("replay-3"),
            trace_ids=[f"{self.agent_id}-t3", f"{self.agent_id}-t1", "not-in-session"],
        )

        self.assertEqual(self._names(turns), ["t1", "t3"])
        # Another session's turn is not returned even when named.
        self.assertEqual(
            await self.store.get_replay_turns(
                self._sid("replay-other"), trace_ids=[f"{self.agent_id}-t1"]
            ),
            [],
        )

    async def test_replay_turns_start_at_the_latest_summary(self) -> None:
        await self._replay_fixture(
            self._sid("replay-2"),
            [
                ("t1", True, False),
                ("sum-old", True, True),
                ("t2", True, False),
                ("sum-new", True, True),
                ("t3", True, False),
                ("wf", False, False),
                ("t4", True, False),
                ("t5", True, False),
            ],
        )

        self.assertEqual(
            self._names(await self.store.get_replay_turns(self._sid("replay-2"))),
            ["sum-new", "t3", "t4", "t5"],
        )
        # With a turn count, the summary and the last turns after it.
        self.assertEqual(
            self._names(await self.store.get_replay_turns(self._sid("replay-2"), turns=2)),
            ["sum-new", "t4", "t5"],
        )
        self.assertEqual(
            self._names(await self.store.get_replay_turns(self._sid("replay-2"), turns=0)),
            ["sum-new"],
        )

    async def test_replay_turns_with_a_count_are_the_last_turns(self) -> None:
        await self._replay_fixture(
            self._sid("replay-4"),
            [("t1", True, False), ("t2", True, False), ("wf", False, False), ("t3", True, False)],
        )

        self.assertEqual(
            self._names(await self.store.get_replay_turns(self._sid("replay-4"), turns=2)),
            ["t2", "t3"],
        )

    async def test_append_entry_is_a_no_op_for_an_existing_trace_id(self) -> None:
        trace_id = f"replayed-{uuid.uuid4().hex}"
        for total in (42, 99):
            await self.store.append_entry(
                trace_id=trace_id,
                session_id="session-1",
                agent_id=self.agent_id,
                user_message="hi",
                agent_response="hello",
                signals={"sentiment": "positive"},
                context_tokens=total,
            )

        turns = await self.store.get_turns(session_id="session-1", agent_id=self.agent_id)
        self.assertEqual([turn["trace_id"] for turn in turns], [trace_id])
        self.assertEqual(turns[0]["context_tokens"], 42)

    async def test_save_and_get_logs_reconstruct_public_event_shape(self) -> None:
        await self.store.save_logs(
            [
                normalize_log_event(
                    CommandEvent(
                        event_type="command.start",
                        agent_id=self.agent_id,
                        session_id="session-1",
                        trace_id="trace-1",
                        command_name="/help",
                        payload={"user_message": "hello"},
                        ts=123.0,
                    )
                )
            ]
        )

        events = await self.store.get_logs(agent_id=self.agent_id, session_id="session-1")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_type"], "command.start")
        self.assertEqual(event["ts"], 123.0)
        self.assertEqual(event["trace_id"], "trace-1")
        self.assertEqual(event["payload"], {"user_message": "hello"})
        self.assertEqual(event["command_name"], "/help")
        self.assertIn("duration_ms", event)
        self.assertIsNone(event["duration_ms"])
        self.assertIn("log_id", event)

    async def test_latest_and_recent_log_traces_use_log_table(self) -> None:
        await self.store.save_logs(
            [
                {
                    "agent_id": self.agent_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "agent_id": self.agent_id,
                    "session_id": "session-1",
                    "trace_id": "trace-2",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
                {
                    "agent_id": self.agent_id,
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
            agent_id=self.agent_id,
            session_id="session-1",
        )
        recent = await self.store.list_recent_log_traces(
            agent_id=self.agent_id,
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
                    "agent_id": self.agent_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "agent_id": self.agent_id,
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.complete",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
            ]
        )

        initial = await self.store.get_logs(agent_id=self.agent_id)
        assert initial
        tail = await self.store.get_logs(
            agent_id=self.agent_id,
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
            context_tokens=9,
        )
        turn_2 = await self._save_turn(
            session_id="session-a",
            user_message="second user",
            agent_response="second agent",
            metadata={"trace_id": "trace-2"},
            context_tokens=11,
        )

        turns = await self.store.get_turns(
            session_id="session-a",
            agent_id=self.agent_id,
            limit=None,
        )
        limited = await self.store.get_turns(
            session_id="session-a",
            agent_id=self.agent_id,
            limit=1,
        )

        self.assertEqual([turn["trace_id"] for turn in turns], [turn_1, turn_2])
        self.assertEqual([turn["trace_id"] for turn in limited], [turn_2])
        self.assertEqual(turns[0]["signals"]["unused_tool_tokens"], 42)
        self.assertEqual(turns[0]["signals"]["unused_tools"], ["alpha", "beta"])
        self.assertEqual(turns[0]["metadata"], {"trace_id": "trace-1"})
        self.assertEqual(turns[1]["context_tokens"], 11)

    async def test_list_workflow_turns_filters_by_app_and_workflow(self) -> None:
        run_id = "mw:h_TI1UUyBX5w8Q:masher-trace-digest:bHfMwMfMsPDPHI60"
        turn_id = await self._save_turn(
            session_id="session-1",
            user_message="digest input",
            agent_response="digest output",
            workflow_id="masher-trace-digest",
            workflow_run_id=run_id,
            task_id="digest-traces",
        )
        await self._save_turn(
            session_id="session-2",
            user_message="other workflow",
            agent_response="ignore",
            workflow_id="other",
            workflow_run_id="mw:h_1:other:r1",
            task_id="digest-traces",
        )
        await self._save_turn(
            session_id="session-1",
            agent_id=self.other_agent_id,
            user_message="other app",
            agent_response="ignore",
            workflow_id="masher-trace-digest",
            workflow_run_id=run_id,
            task_id="digest-traces",
        )

        turns = await self.store.list_workflow_turns(
            agent_id=self.agent_id,
            workflow_id="masher-trace-digest",
            limit=10,
            sort_desc=False,
        )

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["trace_id"], turn_id)
        self.assertEqual(turns[0]["workflow_run_id"], run_id)
        self.assertEqual(turns[0]["task_id"], "digest-traces")
        self.assertEqual(turns[0]["user_message"], "digest input")
        self.assertEqual(turns[0]["agent_response"], "digest output")

    async def test_get_turns_honors_optional_agent_id_filter(self) -> None:
        await self._save_turn(
            session_id="shared-session",
            user_message="from app a",
            agent_response="a",
            agent_id=self.agent_id,
        )
        await self._save_turn(
            session_id="shared-session",
            user_message="from app b",
            agent_response="b",
            agent_id=self.other_agent_id,
        )

        filtered = await self.store.get_turns(
            session_id="shared-session",
            agent_id=self.agent_id,
            limit=None,
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["user_message"], "from app a")
        other = await self.store.get_turns(
            session_id="shared-session",
            agent_id=self.other_agent_id,
            limit=None,
        )
        self.assertEqual(len(other), 1)

    async def test_get_session_signals_returns_chronological_turn_rows(self) -> None:
        first = await self._save_turn(
            session_id="session-a",
            user_message="first",
            agent_response="one",
            signals={"unused_tool_tokens": 42, "unused_tools": ["alpha"]},
        )
        second = await self._save_turn(
            session_id="session-a",
            user_message="second",
            agent_response="two",
            signals={},
        )
        await self._save_turn(
            session_id="session-a",
            user_message="other app",
            agent_response="skip",
            agent_id=self.other_agent_id,
            signals={"unused_tool_tokens": 7},
        )

        rows = await self.store.get_session_signals(
            session_id="session-a",
            agent_id=self.agent_id,
            limit=None,
        )
        limited = await self.store.get_session_signals(
            session_id="session-a",
            agent_id=self.agent_id,
            limit=1,
        )

        self.assertEqual([row["trace_id"] for row in rows], [first, second])
        self.assertEqual(rows[0]["signals"]["unused_tool_tokens"], 42)
        self.assertEqual(rows[0]["signals"]["unused_tools"], ["alpha"])
        self.assertEqual(rows[1]["signals"], {})
        self.assertEqual([row["trace_id"] for row in limited], [second])

    async def test_list_sessions_and_latest_session_are_app_scoped(self) -> None:
        await self._save_turn(
            session_id="session-a",
            user_message="first",
            agent_response="one",
            context_tokens=3,
        )
        await self._save_turn(
            session_id="session-b",
            user_message="second",
            agent_response="two",
            context_tokens=7,
        )
        await self._save_turn(
            session_id="session-x",
            user_message="other app",
            agent_response="other response",
            agent_id=self.other_agent_id,
            context_tokens=99,
        )

        sessions = await self.store.list_sessions(agent_id=self.agent_id, limit=10)
        latest = await self.store.get_latest_session(agent_id=self.agent_id)

        self.assertEqual([item["session_id"] for item in sessions], ["session-b", "session-a"])
        assert latest is not None
        self.assertEqual(latest["session_id"], "session-b")
        self.assertEqual(latest["context_tokens"], 7)

    async def test_list_sessions_filters_by_app_and_carries_the_first_message(self) -> None:
        await self._save_turn(
            session_id="s-a", user_message="first of a " + "x" * 300,
            agent_response="one", app_id="app-1",
        )
        await self._save_turn(
            session_id="s-a", user_message="second of a", agent_response="two", app_id="app-1"
        )
        await self._save_turn(
            session_id="s-b", user_message="first of b", agent_response="one", app_id="app-2"
        )

        everyone = await self.store.list_sessions(agent_id=self.agent_id, limit=10)
        mine = await self.store.list_sessions(agent_id=self.agent_id, limit=10, app_id="app-1")
        nobody = await self.store.list_sessions(agent_id=self.agent_id, limit=10, app_id="app-9")

        self.assertEqual([s["session_id"] for s in everyone], ["s-b", "s-a"])
        self.assertEqual(everyone[0]["first_message"], "first of b")
        self.assertEqual([s["session_id"] for s in mine], ["s-a"])
        # The first turn's message, clipped.
        self.assertTrue(mine[0]["first_message"].startswith("first of a xxx"))
        self.assertEqual(len(mine[0]["first_message"]), 200)
        self.assertEqual(mine[0]["turn_count"], 2)
        self.assertEqual(nobody, [])

    async def test_session_rows_count_each_turn_once_and_page_by_activity(self) -> None:
        for session_id, tokens in [("s-1", 5), ("s-2", 1), ("s-1", 9), ("s-3", 2)]:
            await self._save_turn(
                session_id=session_id,
                user_message="u",
                agent_response="a",
                context_tokens=tokens,
            )
        # Saving a turn again (a replayed turn.persist) changes nothing.
        await self.store.append_entry(
            trace_id="turn-3",
            session_id="s-1",
            agent_id=self.agent_id,
            user_message="u",
            agent_response="a",
            signals={},
            context_tokens=100,
        )

        sessions = await self.store.list_sessions(agent_id=self.agent_id, limit=10)
        self.assertEqual([s["session_id"] for s in sessions], ["s-3", "s-1", "s-2"])
        s1 = sessions[1]
        self.assertEqual((s1["turn_count"], s1["context_tokens"]), (2, 9))
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            newest_s1 = conn.execute(
                "SELECT MAX(created_at) FROM memory_turns WHERE agent_id = %s "
                "AND session_id = 's-1'",
                (self.agent_id,),
            ).fetchone()[0]
        self.assertEqual(s1["last_activity_at"], newest_s1)

        page = await self.store.list_sessions(agent_id=self.agent_id, limit=2)
        self.assertEqual([s["session_id"] for s in page], ["s-3", "s-1"])

    async def test_migration_backfills_session_rows_from_turns(self) -> None:
        await self._save_turn(
            session_id="old", user_message="u", agent_response="a", context_tokens=4
        )
        await self._save_turn(
            session_id="old", user_message="u", agent_response="a", context_tokens=6
        )
        migration = (
            Path(__file__).resolve().parents[3]
            / "src/mash/storage/migrations/018_memory_session.sql"
        ).read_text()
        start = migration.index("INSERT INTO memory_session")
        # 018 predates the agent_id rename (019) and the context_tokens rename (025).
        backfill = (
            migration[start: migration.index(";", start)]
            .replace("app_id", "agent_id")
            .replace("session_total_tokens", "context_tokens")
            .replace("GROUP BY", "WHERE agent_id = %s GROUP BY")
        )
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM memory_session WHERE agent_id = %s", (self.agent_id,))
            conn.execute(backfill, (self.agent_id,))

        [row] = await self.store.list_sessions(agent_id=self.agent_id, limit=10)
        self.assertEqual(
            (row["session_id"], row["turn_count"], row["context_tokens"]),
            ("old", 2, 6),
        )

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
            agent_id=self.agent_id,
            session_id="session-a",
            limit=2,
        )
        latest = await self.store.get_latest_trace(
            agent_id=self.agent_id,
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
                {"session_id": "s2", "trace_id": turn_2},
                {"session_id": "s1", "trace_id": "missing"},
                {"session_id": "s1", "trace_id": turn_1},
            ],
            agent_id=self.agent_id,
        )

        self.assertIsNotNone(turns)
        assert turns is not None
        self.assertEqual(
            [(turn["session_id"], turn["trace_id"]) for turn in turns],
            [("s2", turn_2), ("s1", turn_1)],
        )

    async def test_get_turn_reads_one_turn_by_trace_id(self) -> None:
        trace_id = await self._save_turn(
            session_id="s1", user_message="brief", agent_response="answer"
        )

        turn = await self.store.get_turn(trace_id, self.agent_id)
        self.assertEqual(
            (turn["session_id"], turn["user_message"], turn["agent_response"]),
            ("s1", "brief", "answer"),
        )
        self.assertIsNotNone(await self.store.get_turn(trace_id, self.agent_id, session_id="s1"))
        self.assertIsNone(await self.store.get_turn(trace_id, self.agent_id, session_id="s2"))
        self.assertIsNone(await self.store.get_turn(trace_id, self.other_agent_id))
        self.assertIsNone(await self.store.get_turn("missing", self.agent_id))

    async def test_get_turn_by_ids_honors_optional_agent_id_filter(self) -> None:
        turn_a = await self._save_turn(
            session_id="shared-session",
            user_message="from app a",
            agent_response="a",
            agent_id=self.agent_id,
        )
        turn_b = await self._save_turn(
            session_id="shared-session",
            user_message="from app b",
            agent_response="b",
            agent_id=self.other_agent_id,
        )

        filtered = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "trace_id": turn_b}],
            agent_id=self.agent_id,
        )
        matching_a = await self.store.get_turn_by_ids(
            [
                {"session_id": "shared-session", "trace_id": turn_b},
                {"session_id": "shared-session", "trace_id": turn_a},
            ],
            agent_id=self.agent_id,
        )
        matching_b = await self.store.get_turn_by_ids(
            [{"session_id": "shared-session", "trace_id": turn_b}],
            agent_id=self.other_agent_id,
        )

        self.assertIsNone(filtered)
        self.assertIsNotNone(matching_a)
        assert matching_a is not None
        self.assertEqual([turn["trace_id"] for turn in matching_a], [turn_a])
        self.assertIsNotNone(matching_b)
        assert matching_b is not None
        self.assertEqual([turn["trace_id"] for turn in matching_b], [turn_b])

    async def test_search_and_lookup_scope_to_one_app(self) -> None:
        mine = await self._save_turn(
            session_id="s-a", user_message="invoice totals", agent_response="ok", app_id="app-1"
        )
        theirs = await self._save_turn(
            session_id="s-b", user_message="invoice totals", agent_response="ok", app_id="app-2"
        )

        hits = await self.store.keyword_search(
            "user_message", "invoice", limit=10, agent_id=self.agent_id, app_id="app-1"
        )
        self.assertEqual([hit["trace_id"] for hit in hits], [mine])

        turns = await self.store.get_turn_by_ids(
            pairs=[
                {"session_id": "s-a", "trace_id": mine},
                {"session_id": "s-b", "trace_id": theirs},
            ],
            agent_id=self.agent_id,
            app_id="app-1",
        )
        self.assertEqual([turn["trace_id"] for turn in turns or []], [mine])

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
            agent_id=self.agent_id,
            user_message="hello world",
            agent_response="irrelevant response",
        )
        await self._save_turn(
            session_id="s1",
            agent_id=self.agent_id,
            user_message="hello only",
            agent_response="world hello",
        )
        agent_hit = await self._save_turn(
            session_id="s1",
            agent_id=self.agent_id,
            user_message="no match here",
            agent_response="hello world",
        )
        await self._save_turn(
            session_id="s2",
            agent_id=self.other_agent_id,
            user_message="hello world",
            agent_response="hello world",
        )

        user_results = await self.store.keyword_search(
            "user_message",
            "hello world",
            limit=10,
            session_id="s1",
            agent_id=self.agent_id,
        )
        agent_results = await self.store.keyword_search(
            "agent_response",
            "hello world",
            limit=10,
            session_id="s1",
            agent_id=self.agent_id,
        )

        self.assertEqual([hit["trace_id"] for hit in user_results], [user_hit])
        self.assertEqual(user_results[0]["preview"], "hello world")
        self.assertEqual(
            {hit["trace_id"] for hit in agent_results},
            {agent_hit, "turn-2"},
        )
        self.assertNotIn(user_hit, {hit["trace_id"] for hit in agent_results})

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

    async def _entry(
        self,
        session_id: str,
        name: str,
        *,
        agent_id: str | None = None,
        tokens: int = 0,
        summary: bool = False,
        user_message: str | None = None,
        agent_response: str | None = None,
    ) -> int:
        return await self.store.append_entry(
            trace_id=f"{self.agent_id}-{name}",
            session_id=session_id,
            agent_id=agent_id or self.agent_id,
            user_message=user_message if user_message is not None else f"u-{name}",
            agent_response=agent_response if agent_response is not None else f"a-{name}",
            signals={},
            context_tokens=tokens,
            tokens=tokens,
            metadata={"type": "summary"} if summary else None,
        )

    async def test_append_entry_numbers_a_session_and_counts_its_board(self) -> None:
        session_id = f"board-{uuid.uuid4().hex}"
        other_session = f"board-{uuid.uuid4().hex}"

        self.assertEqual(await self._entry(session_id, "e1", tokens=10), 1)
        self.assertEqual(
            await self._entry(session_id, "e2", agent_id=self.other_agent_id, tokens=5), 2
        )
        self.assertEqual(await self._entry(other_session, "x1", tokens=7), 1)
        self.assertEqual(await self._entry(session_id, "s3", tokens=3, summary=True), 3)
        self.assertEqual(await self._entry(session_id, "e4", tokens=4), 4)
        # Appending a stored trace id again returns its number and adds nothing.
        self.assertEqual(await self._entry(session_id, "e2", tokens=99), 2)

        entries = await self.store.read_entries(session_id)

        self.assertEqual([e["seq"] for e in entries], [1, 2, 3, 4])
        self.assertEqual([e["board_tokens"] for e in entries], [10, 15, 3, 7])
        self.assertEqual(entries[1]["agent_id"], self.other_agent_id)
        last = await self.store.last_entry(session_id)
        self.assertEqual((last["seq"], last["board_tokens"]), (4, 7))
        self.assertIsNone(await self.store.last_entry(f"board-{uuid.uuid4().hex}"))

    async def test_read_entries_by_range(self) -> None:
        session_id = f"board-{uuid.uuid4().hex}"
        for index in range(1, 7):
            await self._entry(session_id, f"e{index}")

        async def seqs(**kwargs: Any) -> list[int]:
            return [e["seq"] for e in await self.store.read_entries(session_id, **kwargs)]

        self.assertEqual(await seqs(after=2), [3, 4, 5, 6])
        self.assertEqual(await seqs(after=2, limit=2), [3, 4])
        self.assertEqual(await seqs(before=5), [1, 2, 3, 4])
        self.assertEqual(await seqs(before=5, limit=2), [3, 4])
        self.assertEqual(await seqs(after=1, before=4), [2, 3])

    async def test_search_entries_stays_in_its_session(self) -> None:
        session_id = f"board-{uuid.uuid4().hex}"
        await self._entry(session_id, "e1", user_message="book a flight to lisbon")
        await self._entry(
            session_id, "e2", agent_id=self.other_agent_id, agent_response="the lisbon itinerary"
        )
        await self._entry(session_id, "e3", user_message="something else")
        await self._entry(f"board-{uuid.uuid4().hex}", "x1", user_message="lisbon too")

        results = await self.store.search_entries(session_id, "lisbon")

        self.assertEqual(sorted(r["seq"] for r in results), [1, 2])
        by_seq = {r["seq"]: r for r in results}
        self.assertEqual(by_seq[2]["agent_id"], self.other_agent_id)
        self.assertIn("lisbon", by_seq[1]["snippet"])
        self.assertEqual(await self.store.search_entries(session_id, "  "), [])

    async def test_read_board_starts_at_the_latest_summary(self) -> None:
        session_id = f"board-{uuid.uuid4().hex}"
        await self._entry(session_id, "e1")
        await self._entry(session_id, "s2", summary=True)
        await self._entry(session_id, "e3", agent_id=self.other_agent_id)
        await self._entry(session_id, "s4", summary=True)
        await self._entry(session_id, "e5")

        async def seqs(**kwargs: Any) -> list[int]:
            return [e["seq"] for e in await self.store.read_board(session_id, **kwargs)]

        self.assertEqual(await seqs(), [4, 5])
        # A recovered request reads the board as it was at its context load.
        self.assertEqual(await seqs(up_to=3), [2, 3])
        self.assertEqual(await seqs(up_to=1), [1])
        self.assertEqual(await self.store.read_board(f"board-{uuid.uuid4().hex}"), [])

    async def test_list_board_sessions_counts_every_agents_entries(self) -> None:
        conversation = f"board-{uuid.uuid4().hex}"
        own = f"board-{uuid.uuid4().hex}"
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, host_id, policy_id) VALUES (%s, 'h', 'p')",
                (conversation,),
            )
        self.addCleanup(self._delete_session_row, conversation)
        await self.store.append_entry(
            trace_id=f"{self.agent_id}-c1", session_id=conversation, agent_id=self.agent_id,
            user_message="first message", agent_response="a", signals={},
            context_tokens=4, tokens=4, app_id="app-board",
        )
        await self.store.append_entry(
            trace_id=f"{self.agent_id}-c2", session_id=conversation,
            agent_id=self.other_agent_id, user_message="second", agent_response="b",
            signals={}, context_tokens=6, tokens=6, app_id="app-board",
        )
        # A session outside the sessions table is an agent's own, not a conversation.
        await self._entry(own, "o1")

        rows = await self.store.list_board_sessions(limit=50, app_id="app-board")

        [row] = rows
        self.assertEqual(
            (row["session_id"], row["turn_count"], row["context_tokens"], row["first_message"]),
            (conversation, 2, 10, "first message"),
        )
        self.assertEqual(await self.store.list_board_sessions(limit=50, app_id="nobody"), [])

    def _delete_session_row(self, session_id: str) -> None:
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))

    async def test_board_indexes_exist(self) -> None:
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename IN ('memory_turns', 'sessions')"
                )
            }
        self.assertIn("idx_memory_turns_session_seq", names)
        self.assertIn("sessions_host_policy", names)
        self.assertNotIn("sessions_host", names)

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

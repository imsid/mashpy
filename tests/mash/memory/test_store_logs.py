"""Tests for SQLiteStore structured log persistence."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import aiosqlite

from mash.logging import AgentTraceEvent, EventLogger, LLMEvent
from mash.memory.store import SQLiteStore


class SQLiteStoreLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = SQLiteStore(":memory:")
        self.logger = EventLogger(self.store)

    async def test_save_and_get_logs_reconstruct_public_event_shape(self) -> None:
        await self.logger.emit(
            AgentTraceEvent(
                event_type="agent.run.start",
                app_id="primary",
                session_id="session-1",
                trace_id="trace-1",
                payload={"user_message": "hello"},
                ts=123.0,
            )
        )

        events = await self.store.get_logs(app_id="primary", session_id="session-1")

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

    async def test_stored_payload_omits_none_fields(self) -> None:
        await self.logger.emit(
            AgentTraceEvent(
                event_type="agent.run.start",
                app_id="primary",
                session_id="session-1",
                trace_id="trace-1",
                payload={},
                ts=123.0,
            )
        )

        assert self.store._conn is not None
        cursor = await self.store._conn.execute("SELECT payload FROM logs")
        row = await cursor.fetchone()
        assert row is not None
        payload_json = row[0]
        payload = json.loads(payload_json)

        self.assertEqual(payload["payload"], {})
        self.assertNotIn("step_id", payload)
        self.assertNotIn("duration_ms", payload)
        self.assertNotIn("action_type", payload)

    async def test_latest_and_recent_log_traces_use_log_table(self) -> None:
        await self.store.save_logs(
            [
                {
                    "app_id": "primary",
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": "primary",
                    "session_id": "session-1",
                    "trace_id": "trace-2",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": "primary",
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
            app_id="primary",
            session_id="session-1",
        )
        recent = await self.store.list_recent_log_traces(
            app_id="primary",
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
                    "app_id": "primary",
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.start",
                    "created_at": 1.0,
                    "payload": {"payload": {}},
                },
                {
                    "app_id": "primary",
                    "session_id": "session-1",
                    "trace_id": "trace-1",
                    "event_class": "AgentTraceEvent",
                    "event_type": "agent.run.complete",
                    "created_at": 2.0,
                    "payload": {"payload": {}},
                },
            ]
        )

        initial = await self.store.get_logs(app_id="primary")
        assert initial
        tail = await self.store.get_logs(
            app_id="primary",
            after_log_id=int(initial[0]["log_id"]),
        )

        self.assertEqual([item["event_type"] for item in tail], ["agent.run.complete"])

    async def test_agent_trace_logging_requires_trace_id(self) -> None:
        with self.assertRaises(ValueError):
            await self.logger.emit(
                AgentTraceEvent(
                    event_type="agent.run.start",
                    app_id="primary",
                    session_id="session-1",
                    trace_id=None,
                )
            )

    async def test_llm_logging_requires_trace_id_provider_and_model(self) -> None:
        with self.assertRaises(ValueError):
            await self.logger.emit(
                LLMEvent(
                    event_type="llm.request.start",
                    app_id="primary",
                    session_id="session-1",
                    provider="openai",
                    model="gpt-test",
                    trace_id=None,
                )
            )

    async def test_open_serializes_concurrent_lazy_initialization(self) -> None:
        store = SQLiteStore(Path(self.enterContext(TemporaryDirectory())) / "state.db")
        connect_calls = 0
        first_connect_started = asyncio.Event()
        release_first_connect = asyncio.Event()
        original_connect = aiosqlite.connect

        async def delayed_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
            nonlocal connect_calls
            connect_calls += 1
            if connect_calls == 1:
                first_connect_started.set()
                await release_first_connect.wait()
            return await original_connect(*args, **kwargs)

        try:
            with patch(
                "mash.memory.store.backends.sqlite.store.aiosqlite.connect",
                new=delayed_connect,
            ):
                first = asyncio.create_task(store.open())
                await first_connect_started.wait()
                others = [asyncio.create_task(store.open()) for _ in range(4)]
                await asyncio.sleep(0)
                release_first_connect.set()
                await asyncio.gather(first, *others)

            self.assertEqual(connect_calls, 1)
            self.assertIsNotNone(store._conn)
        finally:
            await store.close()

    async def test_concurrent_save_logs_and_open_on_fresh_db_persists_logs(self) -> None:
        store = SQLiteStore(Path(self.enterContext(TemporaryDirectory())) / "state.db")
        connect_calls = 0
        first_connect_started = asyncio.Event()
        release_first_connect = asyncio.Event()
        original_connect = aiosqlite.connect

        async def delayed_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
            nonlocal connect_calls
            connect_calls += 1
            if connect_calls == 1:
                first_connect_started.set()
                await release_first_connect.wait()
            return await original_connect(*args, **kwargs)

        log_one = {
            "app_id": "primary",
            "session_id": "session-1",
            "trace_id": "trace-1",
            "event_class": "AgentTraceEvent",
            "event_type": "agent.run.start",
            "created_at": 1.0,
            "payload": {"payload": {"step": 1}},
        }
        log_two = {
            "app_id": "primary",
            "session_id": "session-1",
            "trace_id": "trace-2",
            "event_class": "AgentTraceEvent",
            "event_type": "agent.run.complete",
            "created_at": 2.0,
            "payload": {"payload": {"step": 2}},
        }

        try:
            with patch(
                "mash.memory.store.backends.sqlite.store.aiosqlite.connect",
                new=delayed_connect,
            ):
                first = asyncio.create_task(store.save_logs([log_one]))
                await first_connect_started.wait()
                second = asyncio.create_task(store.save_logs([log_two]))
                third = asyncio.create_task(store.open())
                await asyncio.sleep(0)
                release_first_connect.set()
                await asyncio.gather(first, second, third)

            self.assertEqual(connect_calls, 1)
            events = await store.get_logs(app_id="primary", session_id="session-1")
            self.assertEqual(len(events), 2)
            self.assertEqual({event["trace_id"] for event in events}, {"trace-1", "trace-2"})
            self.assertEqual(
                {event["event_type"] for event in events},
                {"agent.run.start", "agent.run.complete"},
            )
        finally:
            await store.close()


if __name__ == "__main__":
    unittest.main()

"""Tests for SQLiteStore structured log persistence."""

from __future__ import annotations

import json
import unittest

from mash.logging import AgentTraceEvent, EventLogger, LLMEvent
from mash.memory.store import SQLiteStore


class SQLiteStoreLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteStore(":memory:")
        self.logger = EventLogger(self.store)

    def test_save_and_get_logs_reconstruct_public_event_shape(self) -> None:
        self.logger.emit(
            AgentTraceEvent(
                event_type="agent.run.start",
                app_id="primary",
                session_id="session-1",
                trace_id="trace-1",
                payload={"user_message": "hello"},
                ts=123.0,
            )
        )

        events = self.store.get_logs(app_id="primary", session_id="session-1")

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

    def test_stored_payload_omits_none_fields(self) -> None:
        self.logger.emit(
            AgentTraceEvent(
                event_type="agent.run.start",
                app_id="primary",
                session_id="session-1",
                trace_id="trace-1",
                payload={},
                ts=123.0,
            )
        )

        payload_json = self.store._conn.execute("SELECT payload FROM logs").fetchone()[0]
        payload = json.loads(payload_json)

        self.assertEqual(payload["payload"], {})
        self.assertNotIn("step_id", payload)
        self.assertNotIn("duration_ms", payload)
        self.assertNotIn("action_type", payload)

    def test_latest_and_recent_log_traces_use_log_table(self) -> None:
        self.store.save_logs(
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

        latest = self.store.get_latest_log_trace(app_id="primary", session_id="session-1")
        recent = self.store.list_recent_log_traces(app_id="primary", session_id="session-1", limit=2)

        assert latest is not None
        self.assertEqual(latest["trace_id"], "trace-2")
        self.assertEqual(latest["event_count"], 2)
        self.assertEqual([item["trace_id"] for item in recent], ["trace-2", "trace-1"])

    def test_get_logs_supports_cursoring_with_after_log_id(self) -> None:
        self.store.save_logs(
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

        initial = self.store.get_logs(app_id="primary")
        assert initial
        tail = self.store.get_logs(app_id="primary", after_log_id=int(initial[0]["log_id"]))

        self.assertEqual([item["event_type"] for item in tail], ["agent.run.complete"])

    def test_agent_trace_logging_requires_trace_id(self) -> None:
        with self.assertRaises(ValueError):
            self.logger.emit(
                AgentTraceEvent(
                    event_type="agent.run.start",
                    app_id="primary",
                    session_id="session-1",
                    trace_id=None,
                )
            )

    def test_llm_logging_requires_trace_id_provider_and_model(self) -> None:
        with self.assertRaises(ValueError):
            self.logger.emit(
                LLMEvent(
                    event_type="llm.request.start",
                    app_id="primary",
                    session_id="session-1",
                    provider="openai",
                    model="gpt-test",
                    trace_id=None,
                )
            )


if __name__ == "__main__":
    unittest.main()

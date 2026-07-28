"""Tests for Phase 2 request cancellation.

The DBOS-specific cancel-at-step-boundary behavior needs a live DBOS + Postgres.
Here we cover the parts the engine composes: the pending-interaction finder, the
public-event mapping, the terminal cancelled event flipping a request terminal,
and cancel's idempotence — driven through the runtime surface against the inline
test engine.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.engine.steps import open_interaction
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import find_pending_interaction, to_public_event
from mash.testing.runtime_fixtures import build_spec


def _test_stores() -> dict:
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


def _evt(event_type: str, interaction_id: str | None = None) -> RuntimeEvent:
    payload = {"interaction_id": interaction_id} if interaction_id else {}
    return RuntimeEvent(
        app_id="a",
        agent_id="a",
        event_type=event_type,
        request_id="r",
        payload=payload,
    )


class FindPendingInteractionTests(unittest.TestCase):
    def test_none_when_no_interactions(self) -> None:
        events = [_evt(RuntimeEventType.LLM_THINK_STARTED.value)]
        self.assertIsNone(find_pending_interaction(events))

    def test_returns_unacked_create(self) -> None:
        events = [_evt(RuntimeEventType.INTERACTION_CREATE.value, "itr_1")]
        self.assertEqual(find_pending_interaction(events), "itr_1")

    def test_acked_interaction_is_not_pending(self) -> None:
        events = [
            _evt(RuntimeEventType.INTERACTION_CREATE.value, "itr_1"),
            _evt(RuntimeEventType.INTERACTION_ACK.value, "itr_1"),
        ]
        self.assertIsNone(find_pending_interaction(events))

    def test_last_open_create_wins(self) -> None:
        events = [
            _evt(RuntimeEventType.INTERACTION_CREATE.value, "itr_1"),
            _evt(RuntimeEventType.INTERACTION_ACK.value, "itr_1"),
            _evt(RuntimeEventType.INTERACTION_CREATE.value, "itr_2"),
        ]
        self.assertEqual(find_pending_interaction(events), "itr_2")


class PublicEventTests(unittest.TestCase):
    def test_cancelled_maps_to_public_event(self) -> None:
        event = RuntimeEvent(
            app_id="a",
            agent_id="a",
            event_type=RuntimeEventType.REQUEST_CANCELLED.value,
            request_id="r",
            payload={"request_id": "r", "status": "cancelled"},
        )
        public = to_public_event(event)
        self.assertEqual(public["event"], "request.cancelled")
        self.assertEqual(public["data"]["status"], "cancelled")


class CancelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._runtime_database = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        self._runtime_database.start()
        self.addCleanup(self._runtime_database.stop)

    async def _runtime(self) -> AgentRuntime:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="cancel-app"),
            session_id="s-1",
            **_test_stores(),
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def test_cancel_parked_interaction_acks_and_terminates(self) -> None:
        runtime = await self._runtime()
        # Seed a request parked on an interaction: accepted, trace, open create.
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id="req-1",
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                trace_id="trace-1",
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key="request.accepted",
            )
        )
        interaction_id = await open_interaction(
            runtime.app_id,
            "req-1",
            "s-1",
            "trace-1",
            interaction_type="approval",
            prompt="Approve?",
        )

        result = await runtime.cancel_request("req-1")
        self.assertEqual(result["status"], "cancelled")

        events = await runtime.runtime_store.list_request_events("req-1")
        acks = [
            e
            for e in events
            if e.event_type == RuntimeEventType.INTERACTION_ACK.value
        ]
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0].payload.get("interaction_id"), interaction_id)
        self.assertTrue(acks[0].payload.get("cancelled"))

        self.assertEqual(
            events[-1].event_type, RuntimeEventType.REQUEST_CANCELLED.value
        )
        self.assertTrue(await runtime.runtime_store.is_request_terminal("req-1"))

    async def test_cancel_running_request_stream_ends_cancelled(self) -> None:
        runtime = await self._runtime()
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id="req-2",
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                trace_id="trace-2",
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key="request.accepted",
            )
        )
        await runtime.cancel_request("req-2")

        events, _, done = await runtime.stream_response_events(
            "req-2", cursor=0, wait_timeout=0.0
        )
        self.assertTrue(done)
        self.assertEqual(events[-1]["event"], "request.cancelled")

    async def test_cancel_completed_request_is_idempotent(self) -> None:
        runtime = await self._runtime()
        accepted = await runtime.submit_request(message="hi", session_id="s-1")
        request_id = str(accepted["request_id"])
        # Drain to completion.
        cursor, done = 0, False
        while not done:
            _, cursor, done = await runtime.stream_response_events(
                request_id, cursor=cursor, wait_timeout=1.0
            )

        result = await runtime.cancel_request(request_id)
        self.assertNotEqual(result["status"], "cancelled")
        self.assertIn("terminal", result["message"])


if __name__ == "__main__":
    unittest.main()

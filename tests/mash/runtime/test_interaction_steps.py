"""Unit tests for the interaction step primitives.

``open_interaction`` mints the id and emits the create event, and the ack
carries the ``cancelled`` marker. The loop's waiting, recovery and cancel
behavior is covered in ``test_request_engine.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.engine.steps import emit_interaction_ack, open_interaction
from mash.runtime.events import RuntimeEventType
from mash.testing.runtime_fixtures import build_spec


def _test_stores() -> dict:
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


class InteractionStepEventTests(unittest.IsolatedAsyncioTestCase):
    async def _with_runtime(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["MASH_DATA_DIR"] = tmp.name
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="itr-app"),
            session_id="s-1",
            **_test_stores(),
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def test_open_interaction_mints_id_and_emits_create(self) -> None:
        runtime = await self._with_runtime()
        interaction_id = await open_interaction(
            runtime,
            "req-1",
            "s-1",
            "trace-1",
            interaction_type="choice",
            prompt="Pick one",
            options=["a", "b"],
            timeout_seconds=120,
        )
        self.assertTrue(interaction_id.startswith("itr_"))

        events = await runtime.runtime_store.list_request_events("req-1")
        creates = [
            e
            for e in events
            if e.event_type == RuntimeEventType.INTERACTION_START.value
        ]
        self.assertEqual(len(creates), 1)
        payload = creates[0].payload or {}
        self.assertEqual(payload["interaction_id"], interaction_id)
        self.assertEqual(payload["type"], "choice")
        self.assertEqual(payload["prompt"], "Pick one")
        self.assertEqual(payload["schema"]["options"], ["a", "b"])

    async def test_open_interaction_ids_are_unique_per_call(self) -> None:
        runtime = await self._with_runtime()
        first = await open_interaction(
            runtime, "req-2", "s-1", "trace-1",
            interaction_type="info", prompt="?",
        )
        second = await open_interaction(
            runtime, "req-2", "s-1", "trace-1",
            interaction_type="info", prompt="?",
        )
        self.assertNotEqual(first, second)

    async def test_ack_records_cancelled_marker(self) -> None:
        runtime = await self._with_runtime()
        await emit_interaction_ack(
            runtime,
            "req-3",
            "s-1",
            "trace-1",
            interaction_id="itr_abc",
            response=None,
            cancelled=True,
        )
        events = await runtime.runtime_store.list_request_events("req-3")
        acks = [
            e for e in events if e.event_type == RuntimeEventType.INTERACTION_END.value
        ]
        self.assertEqual(len(acks), 1)
        payload = acks[0].payload or {}
        self.assertTrue(payload.get("cancelled"))
        self.assertNotIn("timed_out", payload)

    async def test_ack_without_cancel_has_no_marker(self) -> None:
        runtime = await self._with_runtime()
        await emit_interaction_ack(
            runtime,
            "req-4",
            "s-1",
            "trace-1",
            interaction_id="itr_def",
            response="approve",
        )
        events = await runtime.runtime_store.list_request_events("req-4")
        acks = [
            e for e in events if e.event_type == RuntimeEventType.INTERACTION_END.value
        ]
        payload = acks[0].payload or {}
        self.assertNotIn("cancelled", payload)
        self.assertEqual(payload.get("response"), "approve")


if __name__ == "__main__":
    unittest.main()

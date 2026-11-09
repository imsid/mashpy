"""Unit tests for the interaction step primitives.

Interaction ids derive from their keys, the create event is emitted once per
id, and the ack carries the ``cancelled`` marker. The loop's waiting, recovery and cancel
behavior is covered in ``test_request_engine.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.engine.steps import (
    INTERACTION_SCHEMAS,
    derive_interaction_id,
    emit_interaction_ack,
    emit_interaction_create,
    interaction_response_error,
    interaction_schema,
)
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

    async def test_create_is_emitted_once_per_id(self) -> None:
        runtime = await self._with_runtime()
        for _ in range(2):
            await emit_interaction_create(
                runtime,
                "req-1",
                "s-1",
                "trace-1",
                interaction_id="itr_one",
                interaction_type="choice",
                prompt="Pick one",
                schema=interaction_schema("choice", ["a", "b"]),
                timeout_seconds=120,
                extra={"origin": {"agent_id": "research"}},
            )

        events = await runtime.runtime_store.list_request_events("req-1")
        creates = [
            e
            for e in events
            if e.event_type == RuntimeEventType.INTERACTION_START.value
        ]
        self.assertEqual(len(creates), 1)
        payload = creates[0].payload or {}
        self.assertEqual(payload["interaction_id"], "itr_one")
        self.assertEqual(payload["type"], "choice")
        self.assertEqual(payload["prompt"], "Pick one")
        self.assertEqual(payload["schema"]["options"], ["a", "b"])
        self.assertEqual(payload["origin"], {"agent_id": "research"})

    def test_ids_derive_from_their_keys(self) -> None:
        first = derive_interaction_id("req-1:interaction.open.0.0")
        self.assertTrue(first.startswith("itr_"))
        self.assertEqual(len(first), len("itr_") + 12)
        self.assertEqual(first, derive_interaction_id("req-1:interaction.open.0.0"))
        self.assertNotEqual(first, derive_interaction_id("req-1:interaction.open.0.1"))
        self.assertNotEqual(first, derive_interaction_id("req-2:interaction.open.0.0"))

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



class InteractionResponseTests(unittest.TestCase):
    def test_each_schema_takes_only_its_own_answers(self) -> None:
        choice = INTERACTION_SCHEMAS["choice"](["red", "blue"])
        cases = [
            (INTERACTION_SCHEMAS["approval"], ["approve", "deny", "skip"], ["no", "Approve", None, ["approve"]]),
            (INTERACTION_SCHEMAS["browser"], ["done", "cancel"], ["ok", "", None]),
            (INTERACTION_SCHEMAS["info"], ["anything", ""], [None, 3, ["x"]]),
            (choice, [[], ["red"], ["red", "blue"]], ["red", ["green"], [1], None]),
        ]
        for schema, allowed, refused in cases:
            for answer in allowed:
                with self.subTest(schema=schema, answer=answer):
                    self.assertIsNone(interaction_response_error(schema, answer))
            for answer in refused:
                with self.subTest(schema=schema, answer=answer):
                    self.assertIsNotNone(interaction_response_error(schema, answer))


if __name__ == "__main__":
    unittest.main()

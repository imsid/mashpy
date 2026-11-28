"""Saving a turn twice stores it once, so a recovered request can save again."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from conftest import build_test_stores
from mash.core.context import Context, Response
from mash.runtime.engine.steps import _persist_turn_payload


def _response(trace_id: str) -> Response:
    return Response(
        text="done",
        context=Context(),
        metadata={"trace_id": trace_id, "token_usage": {"input": 30, "output": 12}},
    )


class PersistTurnReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _, memory_store = build_test_stores()
        self.store = memory_store
        self.runtime = SimpleNamespace(store=memory_store, agent_id="agent-a")

    async def _persist(self, context_tokens: int) -> dict:
        return await _persist_turn_payload(
            self.runtime,
            message="plan it",
            session_id="s-1",
            response=_response("tr-1"),
            signals={},
            compaction_payload={},
            context_tokens=context_tokens,
        )

    async def test_the_turn_records_its_context_size(self) -> None:
        payload = await self._persist(42)

        self.assertEqual(payload["context_tokens"], 42)
        [turn] = await self.store.get_turns(session_id="s-1", agent_id="agent-a")
        self.assertEqual((turn["trace_id"], turn["context_tokens"]), ("tr-1", 42))

    async def test_a_second_save_stores_nothing_new(self) -> None:
        await self._persist(42)
        await self._persist(99)

        [turn] = await self.store.get_turns(session_id="s-1", agent_id="agent-a")
        self.assertEqual(turn["context_tokens"], 42)


if __name__ == "__main__":
    unittest.main()

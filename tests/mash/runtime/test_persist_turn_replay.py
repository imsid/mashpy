"""Replaying the turn.persist step must not fail or double-count tokens."""

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
        self.runtime = SimpleNamespace(store=memory_store, app_id="agent-a")
        await memory_store.save_turn(
            trace_id="earlier",
            session_id="s-1",
            app_id="agent-a",
            user_message="hi",
            agent_response="hello",
            signals={},
            session_total_tokens=100,
        )

    async def _persist(self) -> dict:
        return await _persist_turn_payload(
            self.runtime,
            message="plan it",
            session_id="s-1",
            response=_response("tr-1"),
            signals={},
            compaction_payload={},
        )

    async def test_first_persist_adds_turn_tokens_to_session_total(self) -> None:
        payload = await self._persist()

        self.assertEqual(payload["session_total_tokens"], 142)
        turns = await self.store.get_turns(session_id="s-1", app_id="agent-a")
        self.assertEqual([t["trace_id"] for t in turns][-1], "tr-1")

    async def test_replayed_persist_reuses_stored_turn(self) -> None:
        first = await self._persist()
        replayed = await self._persist()

        self.assertEqual(replayed["session_total_tokens"], first["session_total_tokens"])
        turns = await self.store.get_turns(session_id="s-1", app_id="agent-a")
        self.assertEqual(sum(1 for t in turns if t["trace_id"] == "tr-1"), 1)


if __name__ == "__main__":
    unittest.main()

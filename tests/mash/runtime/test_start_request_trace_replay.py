"""Replaying the request.start step must keep the request's original trace_id."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from conftest import build_test_stores
from mash.runtime.engine.dbos import register_runtime, unregister_runtime
from mash.runtime.engine.steps import start_request_trace
from mash.runtime.events import RuntimeEvent, RuntimeEventType


class StartRequestTraceReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        runtime_store, _ = build_test_stores()
        self.runtime_store = runtime_store
        self.runtime = SimpleNamespace(app_id="agent-a", runtime_store=runtime_store)
        register_runtime(self.runtime)

    async def asyncTearDown(self) -> None:
        unregister_runtime(self.runtime)

    async def test_returns_the_trace_id_minted_on_accept(self) -> None:
        await self.runtime_store.append_event(
            RuntimeEvent(
                app_id="agent-a",
                agent_id="agent-a",
                request_id="req-1",
                trace_id="11111111-2222-3333-4444-555555555555",
                event_type=RuntimeEventType.INVOKE_AGENT_START.value,
                dedupe_key="request.accepted",
            )
        )
        first = await start_request_trace("agent-a", "req-1", "s-1", "hello")
        replayed = await start_request_trace("agent-a", "req-1", "s-1", "hello")

        self.assertEqual(first, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(replayed, first)
        events = await self.runtime_store.list_request_events("req-1")
        started = [
            e for e in events if e.event_type == RuntimeEventType.CONTEXT_LOAD_START.value
        ]
        self.assertEqual([e.trace_id for e in started], [first])


if __name__ == "__main__":
    unittest.main()

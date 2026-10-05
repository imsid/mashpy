"""Replaying the request.start step must keep the request's original trace_id."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from conftest import build_test_stores
from mash.runtime.engine.dbos import register_runtime, unregister_runtime
from mash.runtime.engine.steps import start_request_trace
from mash.runtime.events import RuntimeEventType


class _EventLogger:
    def __init__(self) -> None:
        self.trace_ids: list[str] = []

    async def emit(self, event) -> None:
        self.trace_ids.append(event.trace_id)


class StartRequestTraceReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        runtime_store, _ = build_test_stores()
        self.runtime_store = runtime_store
        self.runtime = SimpleNamespace(
            app_id="agent-a",
            runtime_store=runtime_store,
            event_logger=_EventLogger(),
        )
        register_runtime(self.runtime)

    async def asyncTearDown(self) -> None:
        unregister_runtime(self.runtime)

    async def test_replayed_step_returns_the_stored_trace_id(self) -> None:
        first = await start_request_trace("agent-a", "req-1", "s-1", "hello")
        replayed = await start_request_trace("agent-a", "req-1", "s-1", "hello")

        self.assertEqual(replayed, first)
        events = await self.runtime_store.list_request_events("req-1")
        started = [
            e for e in events if e.event_type == RuntimeEventType.TRACE_STARTED.value
        ]
        self.assertEqual([e.trace_id for e in started], [first])
        self.assertEqual(self.runtime.event_logger.trace_ids, [first, first])


if __name__ == "__main__":
    unittest.main()

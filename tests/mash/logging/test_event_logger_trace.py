"""How an emitted event acquires its trace id.

An LLM provider carries the trace id as an instance field, and the runtime
shares one provider instance across every request an agent serves. Two
requests running at once therefore overwrite each other's value, so the
ambient trace context is the authority when it is set.
"""

from __future__ import annotations

import asyncio
import unittest

from mash.logging.events import LLMEvent
from mash.logging.logger import EventLogger
from mash.logging.trace_context import (
    set_request_id,
    set_session_id,
    set_trace_id,
)


class _CollectingStore:
    def __init__(self) -> None:
        self.events: list = []

    async def append_event(self, event):  # noqa: ANN001 - test double
        self.events.append(event)
        return event


def _llm_event(*, trace_id: str | None) -> LLMEvent:
    return LLMEvent(
        event_type="llm.request.complete",
        app_id="agent-1",
        session_id="session-1",
        provider="test",
        model="test-model",
        trace_id=trace_id,
    )


class EventLoggerTraceResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambient_trace_wins_over_a_stale_event_value(self) -> None:
        store = _CollectingStore()
        logger = EventLogger(store)

        set_trace_id("trace-mine")
        set_request_id("req-mine")
        # The shared provider still holds the trace of whichever request
        # configured it last.
        await logger.emit(_llm_event(trace_id="trace-someone-else"))

        self.assertEqual(store.events[-1].trace_id, "trace-mine")
        self.assertEqual(store.events[-1].request_id, "req-mine")

    async def test_event_value_used_when_no_ambient_trace(self) -> None:
        store = _CollectingStore()
        logger = EventLogger(store)

        set_trace_id(None)
        set_request_id(None)
        await logger.emit(_llm_event(trace_id="trace-explicit"))

        self.assertEqual(store.events[-1].trace_id, "trace-explicit")

    async def test_concurrent_requests_keep_their_own_trace(self) -> None:
        """Two requests sharing one provider instance must not cross-stamp.

        Each task sets its own trace context, then emits with the *other*
        request's trace id on the event, standing in for a provider whose
        instance field was overwritten mid-flight.
        """
        store = _CollectingStore()
        logger = EventLogger(store)

        async def run_request(name: str, stale: str) -> None:
            set_trace_id(f"trace-{name}")
            set_request_id(f"req-{name}")
            set_session_id("session-1")
            await asyncio.sleep(0)  # let the other task interleave
            await logger.emit(_llm_event(trace_id=stale))

        await asyncio.gather(
            run_request("a", stale="trace-b"),
            run_request("b", stale="trace-a"),
        )

        by_request = {e.request_id: e.trace_id for e in store.events}
        self.assertEqual(by_request["req-a"], "trace-a")
        self.assertEqual(by_request["req-b"], "trace-b")


if __name__ == "__main__":
    unittest.main()

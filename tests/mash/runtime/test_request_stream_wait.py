"""A stream read that finds nothing new waits for the next append.

The waiter is registered before the read, so an event appended while the read
is in flight wakes it. Registered after, it missed that event and the stream
sat out its whole wait before returning it.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from conftest import _TestRuntimeStore, seed_lifecycle
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import stream_response_events


def _event() -> RuntimeEvent:
    event_type = RuntimeEventType.INTERACTION_START.value
    return RuntimeEvent(
        agent_id="a",
        event_type=event_type,
        lifecycle=seed_lifecycle(event_type),
        request_id="r",
        payload={"interaction_id": "itr_1"},
    )


class _AppendDuringFirstRead(_TestRuntimeStore):
    """The first read sees the log before an append that commits while it runs."""

    def __init__(self) -> None:
        super().__init__("")
        self.reads = 0

    async def has_request(self, request_id: str) -> bool:
        return request_id == "r"

    async def read_request_stream(self, request_id: str, *, after_seq: int = 0):
        self.reads += 1
        result = await super().read_request_stream(request_id, after_seq=after_seq)
        if self.reads == 1:
            await self.append_event(_event())
        return result


class StreamWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_append_during_the_first_read_wakes_the_stream(self) -> None:
        store = _AppendDuringFirstRead()
        runtime = SimpleNamespace(require_open=lambda: None, runtime_store=store)

        started = time.monotonic()
        events, cursor, done = await asyncio.wait_for(
            stream_response_events(runtime, "r", wait_timeout=5.0), timeout=10.0
        )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([event["seq"] for event in events], [1])
        self.assertEqual((cursor, done, store.reads), (1, False, 2))
        self.assertEqual(store._request_waiters.get("r", set()), set())

    async def test_an_empty_stream_returns_after_its_wait(self) -> None:
        store = _AppendDuringFirstRead()
        store.reads = 1  # no append
        runtime = SimpleNamespace(require_open=lambda: None, runtime_store=store)

        events, cursor, done = await stream_response_events(
            runtime, "r", wait_timeout=0.05
        )

        self.assertEqual((events, cursor, done), ([], 0, False))
        self.assertEqual(store._request_waiters.get("r", set()), set())


if __name__ == "__main__":
    unittest.main()

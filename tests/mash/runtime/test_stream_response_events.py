"""Regression tests for terminal detection in stream_response_events.

``done`` must be derived from the fetched page, not a separate terminal-status
query that can observe REQUEST_COMPLETED written just after the page was read —
otherwise the stream ends on a page that never carried the terminal event and
the client sees "stream ended without a terminal event".
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from mash.runtime.requests import _events_reached_terminal, stream_response_events
from mash.runtime.events import RuntimeEventType


def _ev(event_type: str, seq: int) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        request_seq=seq,
        request_id="req-1",
        agent_id="a",
        session_id="s",
        trace_id="t",
        loop_index=None,
        step_key=None,
        created_at=float(seq),
        payload={"seq": seq},
    )


_COMPLETED = RuntimeEventType.REQUEST_COMPLETED.value
_FAILED = RuntimeEventType.REQUEST_FAILED.value
_TRACE = "runtime.llm.think.completed"


class EventsReachedTerminalTests(unittest.TestCase):
    def test_empty_is_not_terminal(self) -> None:
        self.assertFalse(_events_reached_terminal([]))

    def test_last_non_terminal_is_not_terminal(self) -> None:
        self.assertFalse(_events_reached_terminal([_ev(_TRACE, 1)]))

    def test_last_completed_is_terminal(self) -> None:
        self.assertTrue(_events_reached_terminal([_ev(_TRACE, 1), _ev(_COMPLETED, 2)]))

    def test_last_failed_is_terminal(self) -> None:
        self.assertTrue(_events_reached_terminal([_ev(_FAILED, 1)]))


class _FakeStore:
    def __init__(self, pages: list[list[SimpleNamespace]]) -> None:
        self._pages = pages
        self.calls = 0
        # Always reports terminal — models the race where the status query is
        # fresher than the page. The fix must ignore this and use the page.
        self.terminal = True

    async def has_request(self, _request_id: str) -> bool:
        return True

    async def list_request_events(self, _request_id: str, *, after_seq: int = 0):
        page = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return [e for e in page if (e.request_seq or 0) > after_seq]

    async def is_request_terminal(self, _request_id: str) -> bool:
        return self.terminal


def _fake_runtime(store: _FakeStore) -> Any:
    return SimpleNamespace(require_open=lambda: None, runtime_store=store)


class StreamResponseEventsTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_without_terminal_is_not_done_despite_terminal_status(self) -> None:
        # First page has only a non-terminal event; a stale terminal-status
        # query would say True, but done must stay False until the terminal
        # event is actually in the page.
        store = _FakeStore([[_ev(_TRACE, 1)]])
        events, cursor, done = await stream_response_events(
            _fake_runtime(store), "req-1", cursor=0, wait_timeout=0.0
        )
        self.assertEqual([e["event"] for e in events], ["agent.trace"])
        self.assertEqual(cursor, 1)
        self.assertFalse(done)

    async def test_page_with_terminal_is_done(self) -> None:
        store = _FakeStore([[_ev(_TRACE, 1), _ev(_COMPLETED, 2)]])
        events, cursor, done = await stream_response_events(
            _fake_runtime(store), "req-1", cursor=0, wait_timeout=0.0
        )
        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(cursor, 2)
        self.assertTrue(done)

    async def test_empty_page_terminal_rereads_just_written_terminal(self) -> None:
        # First read is empty (terminal event not yet visible); the status is
        # terminal, so the tail is re-read and the terminal event is delivered.
        store = _FakeStore([[], [_ev(_COMPLETED, 1)]])
        store.terminal = True
        events, cursor, done = await stream_response_events(
            _fake_runtime(store), "req-1", cursor=0, wait_timeout=0.0
        )
        self.assertEqual([e["event"] for e in events], ["request.completed"])
        self.assertEqual(cursor, 1)
        self.assertTrue(done)

    async def test_empty_page_terminal_already_delivered_is_done(self) -> None:
        # Polling past a delivered terminal event: empty tail, terminal status,
        # re-read still empty -> done with no events (the client already got it).
        store = _FakeStore([[]])
        store.terminal = True
        events, cursor, done = await stream_response_events(
            _fake_runtime(store), "req-1", cursor=5, wait_timeout=0.0
        )
        self.assertEqual(events, [])
        self.assertEqual(cursor, 5)
        self.assertTrue(done)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

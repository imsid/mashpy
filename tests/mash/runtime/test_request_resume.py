"""Tests for Phase 4 resume hardening: stale-session guard + resumed event."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.errors import RequestStaleError
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import _session_has_later_turn
from mash.testing.runtime_fixtures import build_spec


def _test_stores() -> dict:
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


class SessionHasLaterTurnTests(unittest.TestCase):
    def test_no_turns(self) -> None:
        self.assertFalse(
            _session_has_later_turn([], accepted_at=100.0, request_trace_id="t")
        )

    def test_later_turn_is_stale(self) -> None:
        turns = [{"trace_id": "other", "replayable": True, "created_at": 200.0}]
        self.assertTrue(
            _session_has_later_turn(turns, accepted_at=100.0, request_trace_id="t")
        )

    def test_own_turn_excluded(self) -> None:
        turns = [{"trace_id": "t", "replayable": True, "created_at": 200.0}]
        self.assertFalse(
            _session_has_later_turn(turns, accepted_at=100.0, request_trace_id="t")
        )

    def test_non_replayable_turn_ignored(self) -> None:
        turns = [{"trace_id": "other", "replayable": False, "created_at": 200.0}]
        self.assertFalse(
            _session_has_later_turn(turns, accepted_at=100.0, request_trace_id="t")
        )

    def test_earlier_turn_ignored(self) -> None:
        turns = [{"trace_id": "other", "replayable": True, "created_at": 50.0}]
        self.assertFalse(
            _session_has_later_turn(turns, accepted_at=100.0, request_trace_id="t")
        )


class ResumeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._db = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        self._db.start()
        self.addCleanup(self._db.stop)

    async def _runtime(self) -> AgentRuntime:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="resume-app"),
            session_id="s-1",
            **_test_stores(),
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def _seed(self, runtime, request_id, *, terminal, trace_id="tr-0"):
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id=request_id,
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                trace_id=trace_id,
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key="request.accepted",
            )
        )
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id=request_id,
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                trace_id=trace_id,
                event_type=terminal,
                dedupe_key="terminal",
            )
        )

    async def test_resume_cancelled_request_emits_resumed_and_reopens(self) -> None:
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-1", terminal=RuntimeEventType.REQUEST_CANCELLED.value
        )
        self.assertTrue(await runtime.runtime_store.is_request_terminal("req-1"))

        result = await runtime.resume_request("req-1")
        self.assertEqual(result["status"], "resumed")

        events = await runtime.runtime_store.list_request_events("req-1")
        self.assertEqual(
            events[-1].event_type, RuntimeEventType.REQUEST_RESUMED.value
        )
        # Flipped back to non-terminal so closed streams re-open.
        self.assertFalse(await runtime.runtime_store.is_request_terminal("req-1"))

        public, _, _ = await runtime.stream_response_events(
            "req-1", cursor=0, wait_timeout=0.0
        )
        self.assertEqual(public[-1]["event"], "request.resumed")

    async def test_resume_failed_request_is_refused(self) -> None:
        """DBOS refuses to resume a workflow that ended in ERROR.

        Its ``resume_workflows`` update excludes SUCCESS and ERROR, so calling
        resume there is a silent no-op. Say so rather than reporting a resume
        that never happened — and never emit ``request.resumed``, which would
        strand the request as non-terminal with nothing executing it.
        """
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-failed", terminal=RuntimeEventType.REQUEST_FAILED.value
        )

        result = await runtime.resume_request("req-failed")
        self.assertEqual(result["status"], "failed")
        self.assertIn("rerun", result["message"])

        events = await runtime.runtime_store.list_request_events("req-failed")
        self.assertFalse(
            any(
                e.event_type == RuntimeEventType.REQUEST_RESUMED.value
                for e in events
            )
        )
        # Stays terminal, so streams do not re-open on a request nothing runs.
        self.assertTrue(
            await runtime.runtime_store.is_request_terminal("req-failed")
        )

    async def test_completed_request_in_moved_on_session_is_not_stale(self) -> None:
        """A completed request has nothing to replay, so staleness cannot apply.

        The stale guard must not preempt the idempotent completed response.
        """
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-4", terminal=RuntimeEventType.REQUEST_COMPLETED.value
        )
        time.sleep(0.01)
        await runtime.store.save_turn(
            "tr-later", "s-1", runtime.app_id, "next", "answer", {}, 0,
        )

        result = await runtime.resume_request("req-4")
        self.assertEqual(result["status"], "completed")

    async def test_resume_rejects_stale_session(self) -> None:
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-2", terminal=RuntimeEventType.REQUEST_CANCELLED.value
        )
        # A newer replayable turn in the same session, from a different trace.
        time.sleep(0.01)
        await runtime.store.save_turn(
            "tr-later", "s-1", runtime.app_id, "next", "answer", {}, 0,
        )

        with self.assertRaises(RequestStaleError):
            await runtime.resume_request("req-2")

        # Workflow untouched: no resumed event appended.
        events = await runtime.runtime_store.list_request_events("req-2")
        self.assertFalse(
            any(
                e.event_type == RuntimeEventType.REQUEST_RESUMED.value
                for e in events
            )
        )

    async def test_resume_completed_request_is_idempotent(self) -> None:
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-3", terminal=RuntimeEventType.REQUEST_COMPLETED.value
        )
        # The request's own completed turn (same trace) must not count as stale.
        await runtime.store.save_turn(
            "tr-0", "s-1", runtime.app_id, "hi", "done", {}, 0,
        )

        result = await runtime.resume_request("req-3")
        self.assertEqual(result["status"], "completed")
        events = await runtime.runtime_store.list_request_events("req-3")
        self.assertFalse(
            any(
                e.event_type == RuntimeEventType.REQUEST_RESUMED.value
                for e in events
            )
        )


if __name__ == "__main__":
    unittest.main()

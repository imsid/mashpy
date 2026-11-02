"""Tests for resume: the stale-session guard and the resumed event."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from conftest import build_test_stores, seed_lifecycle
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
                event_type=RuntimeEventType.INVOKE_AGENT_START.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_START.value),
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
                event_type=RuntimeEventType.INVOKE_AGENT_END.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_END.value, terminal),
                dedupe_key="terminal",
                payload={"status": terminal},
            )
        )

    async def test_resume_cancelled_request_emits_resumed_and_reopens(self) -> None:
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-1", terminal="cancelled"
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

    async def test_resume_failed_request_runs_it_again(self) -> None:
        runtime = await self._runtime()
        await self._seed(runtime, "req-failed", terminal="error")

        result = await runtime.resume_request("req-failed")
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(result["previous_status"], "failed")

        events = await runtime.runtime_store.list_request_events("req-failed")
        self.assertIn(
            RuntimeEventType.REQUEST_RESUMED.value, [e.event_type for e in events]
        )

    async def test_resume_running_request_is_a_no_op(self) -> None:
        runtime = await self._runtime()
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id="req-running",
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                trace_id="tr-r",
                event_type=RuntimeEventType.INVOKE_AGENT_START.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_START.value),
                dedupe_key="request.accepted",
            )
        )

        result = await runtime.resume_request("req-running")

        self.assertEqual(result["status"], "running")
        events = await runtime.runtime_store.list_request_events("req-running")
        self.assertNotIn(
            RuntimeEventType.REQUEST_RESUMED.value, [e.event_type for e in events]
        )

    async def test_completed_request_in_moved_on_session_is_not_stale(self) -> None:
        """A completed request has nothing to replay, so staleness cannot apply.

        The stale guard must not preempt the idempotent completed response.
        """
        runtime = await self._runtime()
        await self._seed(
            runtime, "req-4", terminal="ok"
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
            runtime, "req-2", terminal="cancelled"
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
            runtime, "req-3", terminal="ok"
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

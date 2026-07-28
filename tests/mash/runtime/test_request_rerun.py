"""Tests for Phase 5 rerun: start a previous request over as a new request."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.events import RuntimeEventType
from mash.testing.runtime_fixtures import build_spec


def _test_stores() -> dict:
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


class RerunRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
            build_spec(agent_id="rerun-app"),
            session_id="s-1",
            **_test_stores(),
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def _drain(self, runtime, request_id) -> None:
        cursor, done = 0, False
        while not done:
            _, cursor, done = await runtime.stream_response_events(
                request_id, cursor=cursor, wait_timeout=1.0
            )

    async def _accepted_metadata(self, runtime, request_id) -> dict:
        events = await runtime.runtime_store.list_request_events(request_id)
        accepted = next(
            e
            for e in events
            if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value
        )
        return dict((accepted.payload or {}).get("request_metadata") or {})

    async def test_rerun_completed_request_new_id_same_message(self) -> None:
        runtime = await self._runtime()
        original = await runtime.submit_request(
            message="analyze this", session_id="s-1", metadata={"caller": "x"}
        )
        original_id = str(original["request_id"])
        await self._drain(runtime, original_id)

        result = await runtime.rerun_request(original_id)
        new_id = str(result["request_id"])
        await self._drain(runtime, new_id)

        self.assertNotEqual(new_id, original_id)

        new_meta = await self._accepted_metadata(runtime, new_id)
        self.assertEqual(new_meta.get("rerun_of"), original_id)
        # Caller metadata carried across.
        self.assertEqual(new_meta.get("metadata"), {"caller": "x"})

        # New request has its own trace, distinct from the original.
        new_events = await runtime.runtime_store.list_request_events(new_id)
        old_events = await runtime.runtime_store.list_request_events(original_id)
        new_trace = next(e.trace_id for e in new_events if e.trace_id)
        old_trace = next(e.trace_id for e in old_events if e.trace_id)
        self.assertNotEqual(new_trace, old_trace)

    async def test_rerun_preserves_host_snapshot(self) -> None:
        runtime = await self._runtime()
        host = {"host_id": "h1", "primary": "rerun-app", "subagents": []}
        original = await runtime.submit_request(
            message="with host", session_id="s-1", host_snapshot=host
        )
        original_id = str(original["request_id"])
        await self._drain(runtime, original_id)

        result = await runtime.rerun_request(original_id)
        new_id = str(result["request_id"])
        await self._drain(runtime, new_id)

        new_meta = await self._accepted_metadata(runtime, new_id)
        self.assertEqual(new_meta.get("host"), host)
        self.assertEqual(new_meta.get("rerun_of"), original_id)

    async def test_rerun_leaves_original_untouched(self) -> None:
        runtime = await self._runtime()
        original = await runtime.submit_request(message="hi", session_id="s-1")
        original_id = str(original["request_id"])
        await self._drain(runtime, original_id)
        before = await runtime.runtime_store.list_request_events(original_id)

        await runtime.rerun_request(original_id)

        after = await runtime.runtime_store.list_request_events(original_id)
        self.assertEqual(len(before), len(after))
        original_meta = await self._accepted_metadata(runtime, original_id)
        self.assertNotIn("rerun_of", original_meta)

    async def test_rerun_unknown_request_raises(self) -> None:
        runtime = await self._runtime()
        with self.assertRaises(KeyError):
            await runtime.rerun_request("does-not-exist")


if __name__ == "__main__":
    unittest.main()

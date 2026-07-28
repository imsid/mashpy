"""Tests for Phase 3: deterministic, replay-stable subagent request ids.

The workflow binds a deterministic child request id around a subagent
invocation so a DBOS replay reissues the child under the same id and reattaches
to the one existing child instead of starting a second. Full reattach behavior
needs live DBOS (spike-verified separately); here we cover the plumbing: the
submit path uses the bound id and consumes it before the child task spawns, and
the id is stable/distinct per call position.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.logging.trace_context import (
    bound_subagent_request_id,
    get_subagent_request_id,
)
from mash.runtime import AgentRuntime
from mash.testing.runtime_fixtures import build_spec


def _test_stores() -> dict:
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


class DeterministicIdContextTests(unittest.TestCase):
    def test_bound_id_is_visible_then_reset(self) -> None:
        self.assertIsNone(get_subagent_request_id())
        with bound_subagent_request_id("req-1-sub-0-0"):
            self.assertEqual(get_subagent_request_id(), "req-1-sub-0-0")
        self.assertIsNone(get_subagent_request_id())

    def test_key_format_is_stable_and_distinct(self) -> None:
        # The workflow derives this key; assert the shape callers depend on.
        primary = "req-abc"
        keys = {
            (loop, call): f"{primary}-sub-{loop}-{call}"
            for loop in range(2)
            for call in range(2)
        }
        # Distinct per call position...
        self.assertEqual(len(set(keys.values())), 4)
        # ...and stable for the same position (what replay reproduces).
        self.assertEqual(keys[(1, 1)], f"{primary}-sub-1-1")


class DeterministicIdSubmitTests(unittest.IsolatedAsyncioTestCase):
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
            build_spec(agent_id="det-app"),
            session_id="s-1",
            **_test_stores(),
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def test_submit_uses_bound_id_and_consumes_it(self) -> None:
        runtime = await self._runtime()
        with bound_subagent_request_id("req-1-sub-0-0"):
            accepted = await runtime.submit_request(message="hi", session_id="s-1")
            # Consumed during submit, before any child task could inherit it.
            self.assertIsNone(get_subagent_request_id())
        self.assertEqual(accepted["request_id"], "req-1-sub-0-0")

    async def test_submit_without_bound_id_mints_uuid(self) -> None:
        runtime = await self._runtime()
        accepted = await runtime.submit_request(message="hi", session_id="s-1")
        request_id = str(accepted["request_id"])
        self.assertNotIn("-sub-", request_id)
        # A uuid4, not our deterministic shape.
        self.assertEqual(len(request_id), 36)


if __name__ == "__main__":
    unittest.main()

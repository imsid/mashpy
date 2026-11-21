"""How the runtime starts a request: who started it, and whose session it is."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.testing.runtime_fixtures import build_spec


class RequestStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        database = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        database.start()
        self.addCleanup(database.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    async def _runtime(self) -> AgentRuntime:
        runtime_store, memory_store = build_test_stores()
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="start-app"),
            session_id="s-1",
            runtime_store=runtime_store,
            memory_store=memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def test_a_session_takes_requests_only_for_its_user(self) -> None:
        from mash.runtime.errors import SessionUserMismatchError

        runtime = await self._runtime()
        await runtime.submit_request(message="hi", session_id="s-1", user_id="u-1")

        with self.assertRaises(SessionUserMismatchError):
            await runtime.submit_request(message="hi", session_id="s-1", user_id="u-2")
        await runtime.submit_request(message="again", session_id="s-1", user_id="u-1")
        await runtime.submit_request(message="hi", session_id="s-2", user_id="u-2")

    async def test_submit_records_who_started_it_in_the_accepted_event_and_row(self) -> None:
        runtime = await self._runtime()
        accepted = await runtime.submit_request(
            message="hi", session_id="s-1", user_id="u-1"
        )
        events = await runtime.runtime_store.list_request_events(accepted["request_id"])
        self.assertNotIn("mode", events[0].payload)
        self.assertEqual(events[0].payload["started_by"], "user")
        self.assertIsNone(events[0].payload["parent_request_id"])
        [record] = await runtime.runtime_store.list_session_requests("s-1")
        self.assertEqual(record["started_by"], "user")
        self.assertNotIn("mode", record)


if __name__ == "__main__":
    unittest.main()

"""Replay-stable RunSubAgents child ids.

The request loop binds a prefix fixed by the call's position around each
RunSubAgents step. The tool derives each child's id from it, so a rerun of
the step resubmits the same ids and idempotent submission returns the children
already running.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores
from mash.logging.trace_context import (
    bound_subagent_id_prefix,
    get_subagent_id_prefix,
)
from mash.runtime import AgentRuntime
from mash.runtime.engine import loop as engine_loop
from mash.runtime.engine.loop import RequestRun
from mash.testing.runtime_fixtures import build_spec
from mash.tools.subagent import child_request_id


class ChildIdTests(unittest.TestCase):
    def test_bound_prefix_is_visible_then_reset(self) -> None:
        self.assertIsNone(get_subagent_id_prefix())
        with bound_subagent_id_prefix("req-1-sub-0-0"):
            self.assertEqual(get_subagent_id_prefix(), "req-1-sub-0-0")
        self.assertIsNone(get_subagent_id_prefix())

    def test_child_ids_are_stable_uuid5s_with_distinct_short_ids(self) -> None:
        parent = str(uuid.uuid4())
        ids = [
            child_request_id(f"{parent}-sub-{loop}-{call}", index)
            for loop in range(2)
            for call in range(2)
            for index in range(3)
        ]
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(len({value[:8] for value in ids} | {parent[:8]}), 13)
        for value in ids:
            self.assertEqual(uuid.UUID(value).version, 5)
        self.assertEqual(
            child_request_id(f"{parent}-sub-1-1", 2),
            child_request_id(f"{parent}-sub-1-1", 2),
        )


class LoopBindingTests(unittest.IsolatedAsyncioTestCase):
    async def _bound_prefix(self, tool_name: str) -> Any:
        prefixes: list[Any] = []

        async def _tool_call(*args: Any) -> dict:
            del args
            prefixes.append(get_subagent_id_prefix())
            return {}

        run = RequestRun(
            object(),  # type: ignore[arg-type]
            request_id="req-1",
            message="hi",
            session_id="s-1",
            request_metadata={},
            checkpoint={"trace_id": "trace-1"},
        )
        with patch.object(engine_loop, "run_step_tool_call", _tool_call):
            await run._tool_call(
                {},
                loop_index=2,
                call_index=1,
                tool_call={"id": "c1", "name": tool_name, "arguments": {}},
            )
        [prefix] = prefixes
        return prefix

    async def test_run_subagents_runs_under_its_positional_prefix(self) -> None:
        self.assertEqual(await self._bound_prefix("RunSubAgents"), "req-1-sub-2-1")

    async def test_other_tools_run_without_a_prefix(self) -> None:
        self.assertIsNone(await self._bound_prefix("run_code"))


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
        runtime_store, memory_store = build_test_stores()
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="det-app"),
            session_id="s-1",
            runtime_store=runtime_store,
            memory_store=memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def test_bound_prefix_does_not_reach_submit(self) -> None:
        # RunSubAgents passes each child's id explicitly; the submit path
        # itself never reads the prefix.
        runtime = await self._runtime()
        with bound_subagent_id_prefix("req-1-sub-0-0"):
            accepted = await runtime.submit_request(message="hi", session_id="s-1", user_id="u-1")
        self.assertNotIn("-sub-", accepted["request_id"])
        self.assertEqual(uuid.UUID(accepted["request_id"]).version, 4)

    async def test_resubmitting_a_child_id_returns_the_running_child(self) -> None:
        runtime = await self._runtime()
        child_id = child_request_id("req-1-sub-0-0", 0)
        first = await runtime.submit_request(
            message="hi", session_id="s-1", user_id="u-1", request_id=child_id
        )
        again = await runtime.submit_request(
            message="hi", session_id="s-1", user_id="u-1", request_id=child_id
        )
        self.assertEqual(first["request_id"], again["request_id"])
        accepted = [
            e
            for e in await runtime.runtime_store.list_request_events(child_id)
            if e.event_type == "invoke_agent.start"
        ]
        self.assertEqual(len(accepted), 1)


if __name__ == "__main__":
    unittest.main()

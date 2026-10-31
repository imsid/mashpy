"""H2A 0.4.0 submission: caller request ids, idempotency, and parent mapping."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import RequestIdConflictError, parent_request_metadata
from mash.testing.runtime_fixtures import build_spec


class ParentRequestMetadataTests(unittest.TestCase):
    def test_subagent_parent_maps_to_subagent_keys(self) -> None:
        metadata = parent_request_metadata(
            {
                "kind": "subagent",
                "request_id": "req_1",
                "agent_id": "concierge",
                "session_id": "s-1",
                "invoke_opts": {"mode": "fast"},
                "started_by": "concierge",
                "parent_request_id": "req_1",
            },
            agent_id="research",
        )

        self.assertEqual(metadata["parent"]["started_by"], "concierge")
        self.assertEqual(metadata["parent"]["parent_request_id"], "req_1")
        self.assertEqual(metadata["primary_session_id"], "s-1")
        self.assertEqual(metadata["primary_app_id"], "concierge")
        self.assertEqual(metadata["subagent_id"], "research")
        self.assertEqual(metadata["subagent_invoke_opts"], {"mode": "fast"})
        self.assertEqual(metadata["parent"]["request_id"], "req_1")

    def test_workflow_parent_maps_to_workflow_keys(self) -> None:
        metadata = parent_request_metadata(
            {"kind": "workflow", "workflow_id": "changelog", "run_id": "run_9", "task_id": "summarize"},
            agent_id="writer",
        )

        self.assertEqual(metadata["workflow_id"], "changelog")
        self.assertEqual(metadata["workflow_run_id"], "run_9")
        self.assertEqual(metadata["task_id"], "summarize")

    def test_invalid_parents_raise(self) -> None:
        for parent in (
            "subagent",
            {"kind": "other"},
            {"kind": "subagent", "agent_id": "concierge", "session_id": "s-1"},
            {"kind": "workflow", "workflow_id": "changelog", "run_id": "run_9"},
            {"kind": "subagent", "request_id": "r", "agent_id": "a", "session_id": "s", "invoke_opts": []},
            {"kind": "subagent", "request_id": "r", "agent_id": "a", "session_id": "s"},
            {"kind": "subagent", "request_id": "r", "agent_id": "a", "session_id": "s",
             "started_by": "user", "parent_request_id": "r"},
            {"kind": "subagent", "request_id": "r", "agent_id": "a", "session_id": "s",
             "started_by": "a", "parent_request_id": "other"},
        ):
            with self.subTest(parent=parent):
                with self.assertRaises(ValueError):
                    parent_request_metadata(parent, agent_id="research")


class RequestSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.runtime_store, self.memory_store = build_test_stores()

    async def _runtime(self, agent_id: str) -> AgentRuntime:
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id=agent_id, response_text="hello"),
            session_id="host-session",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def _accepted_events(self, request_id: str) -> list:
        events = await self.runtime_store.list_request_events(request_id)
        return [e for e in events if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value]

    async def test_caller_request_id_is_used(self) -> None:
        runtime = await self._runtime("writer")

        accepted = await runtime.submit_request(
            message="hi", session_id="s-1", request_id="run_9-summarize",
            user_id="u-1",
        )

        self.assertEqual(accepted["request_id"], "run_9-summarize")

    async def test_resubmitting_a_known_request_id_returns_the_existing_request(self) -> None:
        runtime = await self._runtime("writer")

        first = await runtime.submit_request(message="hi", session_id="s-1", request_id="req-a", user_id="u-1")
        second = await runtime.submit_request(message="hi again", session_id="s-1", request_id="req-a", user_id="u-1")

        self.assertEqual(first, second)
        self.assertEqual(len(await self._accepted_events("req-a")), 1)
        self.assertEqual(await runtime.get_accepted_request("req-a"), first)
        self.assertIsNone(await runtime.get_accepted_request("req-unknown"))

    async def test_request_id_owned_by_another_agent_conflicts(self) -> None:
        writer = await self._runtime("writer")
        research = await self._runtime("research")
        await writer.submit_request(message="hi", session_id="s-1", request_id="req-a", user_id="u-1")

        with self.assertRaises(RequestIdConflictError):
            await research.submit_request(message="hi", session_id="s-1", request_id="req-a", user_id="u-1")

    async def test_invalid_request_id_raises(self) -> None:
        runtime = await self._runtime("writer")

        with self.assertRaises(ValueError):
            await runtime.submit_request(message="hi", session_id="s-1", request_id="has space", user_id="u-1")

    async def test_workflow_parent_is_recorded_and_stamped_on_events(self) -> None:
        runtime = await self._runtime("writer")

        await runtime.submit_request(
            message="hi",
            session_id="s-1",
            request_id="run_9-summarize",
            parent={"kind": "workflow", "workflow_id": "changelog", "run_id": "run_9", "task_id": "summarize"},
            user_id="u-1",
        )

        [accepted] = await self._accepted_events("run_9-summarize")
        self.assertEqual(accepted.workflow_id, "changelog")
        self.assertEqual(accepted.workflow_run_id, "run_9")
        request_metadata = accepted.payload["request_metadata"]
        self.assertEqual(request_metadata["task_id"], "summarize")
        self.assertEqual(request_metadata["parent"]["kind"], "workflow")

    async def test_subagent_parent_is_recorded(self) -> None:
        runtime = await self._runtime("research")

        await runtime.submit_request(
            message="find papers",
            session_id="s-1",
            request_id="req_1-sub-0-0",
            parent={
                "kind": "subagent",
                "request_id": "req_1",
                "agent_id": "concierge",
                "session_id": "s-1",
                "started_by": "concierge",
                "parent_request_id": "req_1",
            },
            host_snapshot={"host_id": "assistant", "primary": "concierge", "subagents": ["research"]},
            user_id="u-1",
            mode="bg",
        )

        [accepted] = await self._accepted_events("req_1-sub-0-0")
        self.assertEqual(accepted.payload["mode"], "bg")
        self.assertEqual(accepted.payload["started_by"], "concierge")
        self.assertEqual(accepted.payload["parent_request_id"], "req_1")
        [record] = await runtime.runtime_store.list_child_requests("req_1")
        self.assertEqual(
            (record["request_id"], record["mode"], record["started_by"]),
            ("req_1-sub-0-0", "bg", "concierge"),
        )
        request_metadata = accepted.payload["request_metadata"]
        self.assertEqual(request_metadata["subagent_id"], "research")
        self.assertEqual(request_metadata["primary_app_id"], "concierge")
        self.assertEqual(request_metadata["host"]["host_id"], "assistant")


if __name__ == "__main__":
    unittest.main()

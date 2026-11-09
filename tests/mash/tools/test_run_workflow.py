"""Tests for the RunWorkflow tool."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional

from mash.logging import bound_request_id, bound_subagent_id_prefix
from mash.runtime.client import WorkflowInputError
from mash.tools.workflow import RunWorkflowTool


class _Relay:
    """A fake relay: runs finish after ``polls`` status reads; dedup keys return their run."""

    def __init__(self, *, polls: int = 1, final: str = "completed") -> None:
        self.polls = polls
        self.final = final
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.by_key: Dict[str, str] = {}
        self.posts: list[Dict[str, Any]] = []
        self.reject: Optional[list[Any]] = None

    async def post_workflow_run(self, workflow_id, *, parent_request_id, workflow_input, dedup_key):
        self.posts.append(
            {"workflow_id": workflow_id, "parent": parent_request_id, "input": workflow_input, "dedup_key": dedup_key}
        )
        if self.reject is not None:
            raise WorkflowInputError(self.reject)
        if dedup_key in self.by_key:
            return dict(self.runs[self.by_key[dedup_key]])
        run_id = f"run-{len(self.runs) + 1}"
        self.runs[run_id] = {"run_id": run_id, "status": "queued", "reads": 0, "input": workflow_input}
        if dedup_key:
            self.by_key[dedup_key] = run_id
        return dict(self.runs[run_id])

    async def get_workflow_run(self, workflow_id, run_id, *, parent_request_id):
        run = self.runs[run_id]
        run["reads"] += 1
        if run["reads"] >= self.polls:
            run["status"] = self.final
            if self.final == "completed":
                run["result"] = {"summary": f"v{run['input'].get('versions')}"}
            else:
                run["error"] = "boom"
        return dict(run)


class RunWorkflowToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.relay = _Relay(polls=2)
        self.tool = RunWorkflowTool(relay=self.relay, workflow_ids=["changelog"], poll_seconds=0.0)

    async def _run(self, args: Dict[str, Any], prefix: str = "req-1-wf-0-0"):
        with bound_request_id("req-1"), bound_subagent_id_prefix(prefix):
            result = await self.tool.execute(args)
        return result, (json.loads(result.content) if not result.is_error else None)

    async def test_starts_a_run_and_waits_for_its_result(self) -> None:
        result, payload = await self._run({"workflow_id": "changelog", "input": {"versions": 3}})

        self.assertFalse(result.is_error)
        self.assertEqual(payload, {"run_id": "run-1", "status": "completed", "result": {"summary": "v3"}})
        [post] = self.relay.posts
        self.assertEqual(post["parent"], "req-1")
        self.assertEqual(post["dedup_key"], "req-1-wf-0-0")

    async def test_a_replayed_call_returns_the_same_run(self) -> None:
        _, first = await self._run({"workflow_id": "changelog", "input": {}})
        _, second = await self._run({"workflow_id": "changelog", "input": {}})
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.relay.runs), 1)

    async def test_a_failed_run_returns_its_error(self) -> None:
        self.relay.final = "failed"
        _, payload = await self._run({"workflow_id": "changelog", "input": {}})
        self.assertEqual(payload, {"run_id": "run-1", "status": "failed", "error": "boom"})

    async def test_invalid_input_comes_back_as_an_error_with_the_messages(self) -> None:
        self.relay.reject = [{"loc": ["versions"], "msg": "Input should be less than or equal to 10"}]
        result, _ = await self._run({"workflow_id": "changelog", "input": {"versions": 50}})
        self.assertTrue(result.is_error)
        self.assertIn("less than or equal to 10", result.content)

    async def test_a_timeout_returns_timed_out_with_the_run_id(self) -> None:
        self.relay.polls = 10**9
        self.tool = RunWorkflowTool(relay=self.relay, workflow_ids=["changelog"], poll_seconds=0.3)
        _, payload = await self._run({"workflow_id": "changelog", "input": {}, "timeout_seconds": 1})
        self.assertEqual(payload["status"], "timed_out")
        self.assertEqual(payload["run_id"], "run-1")

    async def test_a_workflow_outside_the_host_and_bad_arguments_are_errors(self) -> None:
        for args in (
            {"workflow_id": "other", "input": {}},
            {"workflow_id": "", "input": {}},
            {"workflow_id": "changelog", "input": "x"},
            {"workflow_id": "changelog", "input": {}, "timeout_seconds": 3601},
        ):
            with self.subTest(args=args):
                result, _ = await self._run(args)
                self.assertTrue(result.is_error)
        self.assertEqual(self.relay.posts, [])

    def test_runs_in_parallel_with_other_calls(self) -> None:
        self.assertTrue(RunWorkflowTool.parallel_safe)


if __name__ == "__main__":
    unittest.main()

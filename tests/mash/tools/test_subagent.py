"""Tests for the RunSubAgents tool."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, AsyncIterator, Dict, Optional

from mash.logging import (
    bound_request_id,
    bound_request_metadata,
    bound_subagent_id_prefix,
    clear_trace_id,
    set_trace_id,
)
from mash.tools.subagent import RunSubAgentsTool, child_request_id


def _completed(request_id: str, text: str, **metadata: Any) -> Dict[str, Any]:
    return {
        "event": "request.completed",
        "data": {
            "request_id": request_id,
            "status": "completed",
            "response": {"text": text, "metadata": dict(metadata)},
        },
    }


class _Children:
    """A fake H2A client for every agent, keyed by child request id.

    Each child's script is a list of events, or a float: seconds to wait
    before completing. Submissions are idempotent on request_id, as the real
    agent server's are.
    """

    def __init__(self) -> None:
        self.submitted: list[Dict[str, Any]] = []
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.scripts: Dict[str, Any] = {}
        self.default_script: Any = None
        self.running = 0
        self.max_running = 0
        self.fail_submit_for: set[str] = set()
        self._counter = 0

    def client(self, agent_id: str) -> "_Client":
        return _Client(self, agent_id)


class _Client:
    def __init__(self, children: _Children, agent_id: str) -> None:
        self._children = children
        self.agent_id = agent_id

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        structured_output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        host: Optional[Dict[str, Any]] = None,
        parent: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
    ) -> str:
        children = self._children
        if self.agent_id in children.fail_submit_for:
            raise RuntimeError(f"agent '{self.agent_id}' is unavailable")
        if request_id is None:
            children._counter += 1
            request_id = f"child-{children._counter}"
        children.submitted.append(
            {
                "agent_id": self.agent_id,
                "message": message,
                "session_id": session_id,
                "structured_output": structured_output,
                "metadata": metadata,
                "request_id": request_id,
                "host": host,
                "parent": parent,
                "mode": mode,
            }
        )
        if request_id not in children.started:
            children.started.append(request_id)
        return request_id

    async def stream_response(
        self, request_id: str, *, timeout: Optional[float] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        children = self._children
        script = children.scripts.get(self.agent_id, children.default_script)
        children.running += 1
        children.max_running = max(children.max_running, children.running)
        try:
            yield {"event": "request.accepted", "data": {"request_id": request_id}}
            if isinstance(script, (int, float)):
                await asyncio.sleep(float(script))
                yield _completed(request_id, f"{self.agent_id} done")
                return
            for event in script or [_completed(request_id, f"{self.agent_id} done")]:
                await asyncio.sleep(0)
                yield event
        finally:
            children.running -= 1

    async def cancel_request(self, request_id: str) -> Dict[str, Any]:
        self._children.cancelled.append(request_id)
        return {"request_id": request_id, "status": "cancelled"}


class _RecordingEventLogger:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class RunSubAgentsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_trace_id()
        self.children = _Children()
        self.tool = RunSubAgentsTool(
            caller_agent_id="planner",
            client_resolver=self.children.client,
            session_id="s1",
            host={"host_id": "assistant", "primary": "planner", "subagents": ["research"]},
        )

    def tearDown(self) -> None:
        clear_trace_id()

    async def _run(self, args: Dict[str, Any]) -> tuple[Any, list[Dict[str, Any]]]:
        with bound_request_id("req-1"), bound_subagent_id_prefix("req-1-sub-0-0"):
            result = await self.tool.execute(args)
        entries = json.loads(result.content) if not result.is_error else []
        return result, entries

    async def test_one_item_hands_a_task_to_a_subagent(self) -> None:
        set_trace_id("trace-primary")
        result, entries = await self._run(
            {"requests": [{"agent_id": "research", "prompt": "find papers"}]}
        )

        child_id = child_request_id("req-1-sub-0-0", 0)
        self.assertFalse(result.is_error)
        self.assertEqual(
            entries,
            [
                {
                    "agent_id": "research",
                    "request_id": child_id,
                    "session_id": child_id,
                    "status": "completed",
                    "response": "research done",
                }
            ],
        )
        [call] = self.children.submitted
        self.assertEqual(call["message"], "find papers")
        # A child without a session_id starts a session named after itself.
        self.assertEqual(call["session_id"], child_id)
        self.assertEqual(call["mode"], "bg")
        self.assertEqual(call["host"]["host_id"], "assistant")
        self.assertEqual(
            call["parent"],
            {
                "kind": "subagent",
                "request_id": "req-1",
                "agent_id": "planner",
                "session_id": "s1",
                "started_by": "planner",
                "parent_request_id": "req-1",
            },
        )
        self.assertEqual(result.metadata["agent_id"], "research")
        self.assertEqual(result.metadata["request_id"], entries[0]["request_id"])
        self.assertEqual(result.metadata["primary_session_id"], "s1")
        self.assertEqual(result.metadata["subagent_session_ids"], [child_id])

    async def test_several_children_run_at_the_same_time_and_report_in_order(self) -> None:
        self.children.scripts = {"research": 0.2, "planner": 0.05}
        items = [
            {"agent_id": "research", "prompt": "part 1"},
            {"agent_id": "planner", "prompt": "part 2"},
            {"agent_id": "research", "prompt": "part 3"},
        ]
        loop = asyncio.get_running_loop()
        started = loop.time()
        result, entries = await self._run({"requests": items})
        elapsed = loop.time() - started

        self.assertFalse(result.is_error)
        self.assertEqual(self.children.max_running, 3)
        self.assertLess(elapsed, 0.4)
        self.assertEqual(
            [(e["agent_id"], e["status"], e["response"]) for e in entries],
            [
                ("research", "completed", "research done"),
                ("planner", "completed", "planner done"),
                ("research", "completed", "research done"),
            ],
        )
        self.assertEqual(
            [e["request_id"] for e in entries],
            [child_request_id("req-1-sub-0-0", i) for i in range(3)],
        )
        self.assertEqual(result.metadata["agent_ids"], ["research", "planner"])

    async def test_one_failure_stays_in_its_entry(self) -> None:
        self.children.scripts = {
            "research": [
                {
                    "event": "request.error",
                    "data": {"error": "boom", "error_code": "tool_error", "retryable": False},
                }
            ],
            "planner": [{"event": "request.cancelled", "data": {}}],
        }
        self.children.fail_submit_for = {"ghost"}
        result, entries = await self._run(
            {
                "requests": [
                    {"agent_id": "research", "prompt": "a"},
                    {"agent_id": "writer", "prompt": "b"},
                    {"agent_id": "planner", "prompt": "c"},
                    {"agent_id": "ghost", "prompt": "d"},
                ]
            }
        )

        self.assertFalse(result.is_error)
        self.assertEqual(
            [e["status"] for e in entries], ["failed", "completed", "cancelled", "failed"]
        )
        self.assertEqual(entries[0]["error"], "boom")
        self.assertEqual(entries[0]["error_code"], "tool_error")
        self.assertEqual(entries[1]["response"], "writer done")
        self.assertIsNone(entries[3]["request_id"])
        self.assertIn("unavailable", entries[3]["error"])

    async def test_the_timeout_cancels_the_children_still_running(self) -> None:
        self.children.scripts = {"research": 30.0, "planner": 0.0}
        result, entries = await self._run(
            {
                "requests": [
                    {"agent_id": "research", "prompt": "slow"},
                    {"agent_id": "planner", "prompt": "fast"},
                ],
                "timeout_seconds": 1,
            }
        )

        self.assertFalse(result.is_error)
        self.assertEqual([e["status"] for e in entries], ["timed_out", "completed"])
        self.assertEqual(self.children.cancelled, [entries[0]["request_id"]])

    async def test_output_schema_becomes_the_childs_structured_output(self) -> None:
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        self.children.default_script = [
            {
                "event": "request.completed",
                "data": {
                    "response": {
                        "text": "scored",
                        "metadata": {"structured_output": {"score": 0.9}},
                    }
                },
            }
        ]
        _, entries = await self._run(
            {"requests": [{"agent_id": "research", "prompt": "score", "output_schema": schema}]}
        )

        self.assertEqual(self.children.submitted[0]["structured_output"], schema)
        self.assertEqual(entries[0]["structured_output"], {"score": 0.9})

    async def test_a_replay_with_the_same_prefix_gets_the_same_children(self) -> None:
        args = {
            "requests": [
                {"agent_id": "research", "prompt": "a"},
                {"agent_id": "planner", "prompt": "b"},
            ]
        }
        _, first = await self._run(args)
        _, second = await self._run(args)

        self.assertEqual(
            [e["request_id"] for e in first], [e["request_id"] for e in second]
        )
        self.assertEqual(
            [e["session_id"] for e in first], [e["session_id"] for e in second]
        )
        self.assertEqual(len(self.children.started), 2)

    async def test_session_id_continues_an_earlier_session(self) -> None:
        _, entries = await self._run(
            {
                "requests": [
                    {"agent_id": "research", "prompt": "follow up", "session_id": "sess-a"},
                    {"agent_id": "research", "prompt": "new work"},
                ]
            }
        )

        sessions = [call["session_id"] for call in self.children.submitted]
        self.assertEqual(sessions, ["sess-a", child_request_id("req-1-sub-0-0", 1)])
        self.assertEqual([e["session_id"] for e in entries], sessions)
        # Both children still name the caller's session as their parent's.
        self.assertEqual(
            {call["parent"]["session_id"] for call in self.children.submitted}, {"s1"}
        )

    async def test_entries_carry_the_childs_trace_id(self) -> None:
        self.children.default_script = [
            {
                "event": "request.completed",
                "data": {"response": {"text": "ok", "metadata": {"trace_id": "t-child"}}},
            }
        ]
        _, entries = await self._run({"requests": [{"agent_id": "research", "prompt": "a"}]})
        self.assertEqual(entries[0]["trace_id"], "t-child")

    async def test_forwards_the_bound_request_metadata(self) -> None:
        with bound_request_metadata({"tenant": "acme"}):
            await self._run({"requests": [{"agent_id": "research", "prompt": "a"}]})
        self.assertEqual(self.children.submitted[0]["metadata"], {"tenant": "acme"})

    async def test_without_a_prefix_each_child_gets_a_fresh_id_and_session(self) -> None:
        with bound_request_id("req-1"):
            result = await self.tool.execute(
                {"requests": [{"agent_id": "research", "prompt": "a"}] * 2}
            )
        entries = json.loads(result.content)
        self.assertEqual(len({e["request_id"] for e in entries}), 2)
        for entry in entries:
            self.assertEqual(entry["session_id"], entry["request_id"])

    async def test_invalid_arguments_are_errors(self) -> None:
        for args in (
            {},
            {"requests": []},
            {"requests": ["research"]},
            {"requests": [{"agent_id": "", "prompt": "a"}]},
            {"requests": [{"agent_id": "research", "prompt": " "}]},
            {"requests": [{"agent_id": "research", "prompt": "a", "output_schema": "x"}]},
            {"requests": [{"agent_id": "research", "prompt": "a", "session_id": " "}]},
            {"requests": [{"agent_id": "research", "prompt": "a", "session_id": 7}]},
            {"requests": [{"agent_id": "research", "prompt": "a"}], "timeout_seconds": 0},
            {"requests": [{"agent_id": "research", "prompt": "a"}], "timeout_seconds": "soon"},
            {"requests": [{"agent_id": "research", "prompt": "a"}], "timeout_seconds": 3601},
            {"requests": [{"agent_id": "research", "prompt": "a"}] * 17},
        ):
            with self.subTest(args=args):
                result = await self.tool.execute(args)
                self.assertTrue(result.is_error)
        self.assertEqual(self.children.submitted, [])

    async def test_the_agents_timeout_is_the_default_and_the_ceiling(self) -> None:
        self.assertEqual(self.tool.parameters["properties"]["timeout_seconds"]["maximum"], 3600)
        tool = RunSubAgentsTool(
            caller_agent_id="planner",
            client_resolver=self.children.client,
            session_id="s1",
            timeout_seconds=1,
        )
        self.children.scripts = {"research": 30.0}
        with bound_request_id("req-1"), bound_subagent_id_prefix("req-1-sub-0-0"):
            result = await tool.execute({"requests": [{"agent_id": "research", "prompt": "slow"}]})
            refused = await tool.execute(
                {"requests": [{"agent_id": "research", "prompt": "a"}], "timeout_seconds": 2}
            )
        self.assertEqual(json.loads(result.content)[0]["status"], "timed_out")
        self.assertTrue(refused.is_error)
        self.assertIn("at most 1", refused.content)
        self.assertEqual(tool.parameters["properties"]["timeout_seconds"]["maximum"], 1)
        # The class schema is untouched by an instance's limit.
        self.assertNotIn("timeout_seconds", RunSubAgentsTool.parameters["properties"])

    def test_configure_run_subagents_uses_the_agents_config(self) -> None:
        from types import SimpleNamespace

        from mash.core.config import AgentConfig
        from mash.runtime.factory import configure_run_subagents
        from mash.tools.registry import ToolRegistry

        access = SimpleNamespace(get_client=lambda agent_id: None)
        runtime = SimpleNamespace(app_id="planner", get_subagent_access=lambda: access)
        for config, expected in (
            (AgentConfig(app_id="planner", system_prompt="p"), 3600),
            (AgentConfig(app_id="planner", system_prompt="p", subagent_timeout_seconds=7200), 7200),
        ):
            agent = SimpleNamespace(tools=ToolRegistry(), config=config)
            configure_run_subagents(runtime, agent, session_id="s1", host=None, subagent_ids=set())
            schema = agent.tools.get("RunSubAgents").parameters["properties"]["timeout_seconds"]
            self.assertEqual(schema["maximum"], expected)

    def test_a_non_positive_timeout_is_refused(self) -> None:
        from mash.core.config import AgentConfig

        with self.assertRaises(ValueError):
            AgentConfig(app_id="a", system_prompt="p", subagent_timeout_seconds=0)

    def test_the_schema_states_the_limits(self) -> None:
        properties = self.tool.parameters["properties"]
        self.assertEqual(properties["requests"]["maxItems"], 16)
        self.assertEqual(properties["timeout_seconds"]["maximum"], 3600)

    async def test_the_limits_themselves_are_accepted(self) -> None:
        result = await self.tool.execute(
            {"requests": [{"agent_id": "research", "prompt": "a"}] * 16, "timeout_seconds": 3600}
        )
        self.assertEqual(len(self.children.submitted), 16, result.content)

    async def test_the_description_names_the_callers_own_id(self) -> None:
        self.assertIn("'planner'", self.tool.description)
        self.assertEqual(self.tool.name, "RunSubAgents")
        self.assertFalse(self.tool.parallel_safe)

    def test_constructor_requires_client_resolver_and_session(self) -> None:
        with self.assertRaises(ValueError):
            RunSubAgentsTool(caller_agent_id="p", client_resolver=None, session_id="s")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RunSubAgentsTool(caller_agent_id="p", client_resolver=lambda _: None, session_id="")


if __name__ == "__main__":
    unittest.main()

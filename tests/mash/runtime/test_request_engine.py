"""The request engine: checkpoints, crash recovery, interactions, cancel and resume.

A crash is an engine closed mid-request: its tasks are cancelled and the
request stays ``running``, as when the agent process dies. A second runtime
over the same stores then plays the restarted process.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores, seed_lifecycle
from mash.runtime import AgentRuntime
from mash.runtime.engine import InteractionClosedError
from mash.runtime.engine.engine import MAX_RECOVERIES
from mash.runtime.engine.steps import emit_interaction_ack
from mash.runtime.events import RequestStatus, RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import ToolCallingAgentSpec
from mash.tools.ask_user import AskUserTool
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry


class _BlockingTool:
    """Counts its runs; blocks until released."""

    name = "work"
    description = "Do some work."
    requires_approval = False
    parallel_safe = False
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.runs = 0
        self.release = asyncio.Event()

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        self.runs += 1
        await self.release.wait()
        return ToolResult.success("worked")

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class _Spec(ToolCallingAgentSpec):
    def __init__(self, tool: Any, *, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(agent_id="engine-app", tool_name=tool_name, arguments=arguments)
        self._tool = tool
        self.model_calls = 0
        send = self.provider.send

        async def _counted(request):
            self.model_calls += 1
            return await send(request)

        self.provider.send = _counted  # type: ignore[method-assign]

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(self._tool)
        return tools


class RequestEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (
            patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name}),
            patch(
                "mash.runtime.service.resolve_database_url",
                return_value="postgresql://test/runtime",
            ),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.runtime_store, self.memory_store = build_test_stores()

    async def _runtime(self, spec: Any) -> AgentRuntime:
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="s-1",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.engine.close)
        return runtime

    async def _events(self, request_id: str, event_type: str) -> list[RuntimeEvent]:
        return [
            e
            for e in await self.runtime_store.list_request_events(request_id)
            if e.event_type == event_type
        ]

    async def _wait_for(self, request_id: str, event_type: str, count: int = 1) -> list[RuntimeEvent]:
        for _ in range(300):
            events = await self._events(request_id, event_type)
            if len(events) >= count:
                return events
            await asyncio.sleep(0.01)
        self.fail(f"{request_id} never reached {count} {event_type}")

    async def _terminal(self, request_id: str) -> RuntimeEvent:
        [end] = await self._wait_for(request_id, RuntimeEventType.INVOKE_AGENT_END.value)
        return end

    def _ask_spec(self) -> _Spec:
        return _Spec(AskUserTool(), tool_name="AskUser", arguments={"question": "Which one?"})

    async def test_a_crashed_request_continues_from_its_checkpoint(self) -> None:
        tool = _BlockingTool()
        spec = _Spec(tool, tool_name="work", arguments={})
        first = await self._runtime(spec)
        accepted = await first.submit_request(message="go", session_id="s-1", user_id="u-1")
        request_id = accepted["request_id"]
        await self._wait_for(request_id, RuntimeEventType.EXECUTE_TOOL_START.value)

        await first.engine.close()  # the process dies mid-tool
        self.assertIs(
            await self.runtime_store.get_request_lifecycle(request_id), RequestStatus.RUNNING
        )

        tool.release.set()
        await self._runtime(spec)  # the restarted process recovers it
        end = await self._terminal(request_id)

        self.assertEqual(end.payload["status"], "ok")
        self.assertEqual(end.payload["response"]["text"], "worked")
        # The tool was mid-call, so it runs again; the model's first turn was
        # checkpointed, so the model is asked only for its final answer.
        self.assertEqual(tool.runs, 2)
        self.assertEqual(spec.model_calls, 2)

    async def test_an_answer_wakes_the_request(self) -> None:
        runtime = await self._runtime(self._ask_spec())
        accepted = await runtime.submit_request(message="ask", session_id="s-1", user_id="u-1")
        request_id = accepted["request_id"]
        [opened] = await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)
        interaction_id = opened.payload["interaction_id"]

        await runtime.answer_interaction(request_id, interaction_id, "the blue one")
        end = await self._terminal(request_id)

        self.assertEqual(end.payload["response"]["text"], "the blue one")
        with self.assertRaises(InteractionClosedError):
            await runtime.answer_interaction(request_id, interaction_id, "another")
        with self.assertRaises(KeyError):
            await runtime.answer_interaction(request_id, "itr_unknown", "x")

    async def test_an_answer_stored_before_a_crash_is_used_on_recovery(self) -> None:
        spec = self._ask_spec()
        first = await self._runtime(spec)
        accepted = await first.submit_request(message="ask", session_id="s-1", user_id="u-1")
        request_id = accepted["request_id"]
        [opened] = await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)
        await first.engine.close()
        await emit_interaction_ack(
            first,
            request_id,
            "s-1",
            opened.trace_id,
            interaction_id=opened.payload["interaction_id"],
            response="answered while down",
        )

        await self._runtime(spec)
        end = await self._terminal(request_id)

        self.assertEqual(end.payload["response"]["text"], "answered while down")
        self.assertEqual(
            len(await self._events(request_id, RuntimeEventType.INTERACTION_START.value)), 1
        )

    async def test_an_unanswered_interaction_times_out_with_the_default(self) -> None:
        with patch("mash.tools.ask_user.ASK_USER_DEFAULT_TIMEOUT_SECONDS", 0):
            runtime = await self._runtime(self._ask_spec())
            accepted = await runtime.submit_request(message="ask", session_id="s-1", user_id="u-1")
            request_id = accepted["request_id"]
            end = await self._terminal(request_id)

        [ack] = await self._events(request_id, RuntimeEventType.INTERACTION_END.value)
        self.assertIs(ack.payload["timed_out"], True)
        self.assertEqual(end.payload["status"], "ok")

    async def test_resume_after_cancel_opens_a_new_interaction(self) -> None:
        runtime = await self._runtime(self._ask_spec())
        accepted = await runtime.submit_request(message="ask", session_id="s-1", user_id="u-1")
        request_id = accepted["request_id"]
        await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)

        cancelled = await runtime.cancel_request(request_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(await self.runtime_store.is_request_terminal(request_id))

        resumed = await runtime.resume_request(request_id)
        self.assertEqual(resumed["status"], "resumed")
        first, second = await self._wait_for(
            request_id, RuntimeEventType.INTERACTION_START.value, count=2
        )
        self.assertNotEqual(first.payload["interaction_id"], second.payload["interaction_id"])

        await runtime.answer_interaction(request_id, second.payload["interaction_id"], "yes")
        ends = await self._wait_for(request_id, RuntimeEventType.INVOKE_AGENT_END.value, count=2)
        self.assertEqual([e.payload["status"] for e in ends], ["cancelled", "ok"])

    async def test_a_request_past_the_recovery_limit_fails(self) -> None:
        await self.runtime_store.append_event(
            RuntimeEvent(
                request_id="req-loop",
                app_id="engine-app",
                agent_id="engine-app",
                session_id="s-1",
                trace_id="trace-loop",
                event_type=RuntimeEventType.INVOKE_AGENT_START.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_START.value),
                dedupe_key="request.accepted",
                payload={"message": "go", "initial_session_id": "s-1"},
            )
        )
        for _ in range(MAX_RECOVERIES):
            await self.runtime_store.count_recovery("req-loop")

        await self._runtime(_Spec(_BlockingTool(), tool_name="work", arguments={}))
        end = await self._terminal("req-loop")

        self.assertEqual(end.payload["status"], "error")
        self.assertEqual(end.payload["error_type"], "recovery_limit")


if __name__ == "__main__":
    unittest.main()

"""MASH_AGENT_MAX_RUNNING: how many requests one agent process works on at once."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

import pytest

from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.engine import loop as engine_loop
from mash.runtime.engine.engine import (
    DEFAULT_AGENT_MAX_RUNNING,
    MASH_AGENT_MAX_RUNNING_ENV,
    agent_max_running,
)
from mash.runtime.engine.loop import RequestCancelled, RequestRun
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import ToolCallingAgentSpec
from mash.tools.ask_user import AskUserTool
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry

CHAT_START = RuntimeEventType.CHAT_START.value
TOOL_START = RuntimeEventType.EXECUTE_TOOL_START.value
INTERACTION_START = RuntimeEventType.INTERACTION_START.value
END = RuntimeEventType.INVOKE_AGENT_END.value


def test_default_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASH_AGENT_MAX_RUNNING_ENV, raising=False)
    assert agent_max_running() == DEFAULT_AGENT_MAX_RUNNING == 32


@pytest.mark.parametrize("raw", ["0", "-2", "many"])
def test_bad_cap_names_the_variable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(MASH_AGENT_MAX_RUNNING_ENV, raw)
    with pytest.raises(ValueError, match=MASH_AGENT_MAX_RUNNING_ENV):
        agent_max_running()


class _BlockingTool:
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
        super().__init__(agent_id="cap-app", tool_name=tool_name, arguments=arguments)
        self._tool = tool

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(self._tool)
        return tools


class RunningCapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (
            patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name, MASH_AGENT_MAX_RUNNING_ENV: "1"}),
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

    async def _wait_for(self, request_id: str, event_type: str) -> RuntimeEvent:
        for _ in range(300):
            events = await self._events(request_id, event_type)
            if events:
                return events[0]
            await asyncio.sleep(0.01)
        self.fail(f"{request_id} never reached {event_type}")

    async def _submit(self, runtime: AgentRuntime, session_id: str) -> str:
        accepted = await runtime.submit_request(message="go", session_id=session_id, user_id="u-1")
        return str(accepted["request_id"])

    async def test_a_request_waits_for_a_slot(self) -> None:
        tool = _BlockingTool()
        runtime = await self._runtime(_Spec(tool, tool_name="work", arguments={}))
        self.assertEqual(runtime.engine.max_running, 1)

        first = await self._submit(runtime, "s-1")
        await self._wait_for(first, TOOL_START)
        second = await self._submit(runtime, "s-2")
        await asyncio.sleep(0.2)
        self.assertEqual(await self._events(second, CHAT_START), [])
        self.assertEqual(tool.runs, 1)

        tool.release.set()
        self.assertEqual((await self._wait_for(first, END)).payload["status"], "ok")
        self.assertEqual((await self._wait_for(second, END)).payload["status"], "ok")
        self.assertEqual(tool.runs, 2)

    async def test_a_request_waiting_on_the_user_holds_no_slot(self) -> None:
        runtime = await self._runtime(
            _Spec(AskUserTool(), tool_name="AskUser", arguments={"question": "Which?"})
        )

        first = await self._submit(runtime, "s-1")
        first_open = await self._wait_for(first, INTERACTION_START)
        # With one slot, the second request asks only if the first gave it back.
        second = await self._submit(runtime, "s-2")
        second_open = await self._wait_for(second, INTERACTION_START)

        await runtime.answer_interaction(first, first_open.payload["interaction_id"], "red")
        await runtime.answer_interaction(second, second_open.payload["interaction_id"], "blue")
        self.assertEqual((await self._wait_for(first, END)).payload["response"]["text"], "red")
        self.assertEqual((await self._wait_for(second, END)).payload["response"]["text"], "blue")

    async def test_cancelling_a_waiting_request_ends_it_without_a_slot(self) -> None:
        tool = _BlockingTool()
        runtime = await self._runtime(_Spec(tool, tool_name="work", arguments={}))
        first = await self._submit(runtime, "s-1")
        await self._wait_for(first, TOOL_START)
        second = await self._submit(runtime, "s-2")

        await runtime.cancel_request(second)

        self.assertEqual((await self._wait_for(second, END)).payload["status"], "cancelled")
        for _ in range(100):
            if second not in runtime.engine._tasks:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(second, runtime.engine._tasks)
        self.assertEqual(await self._events(second, CHAT_START), [])
        tool.release.set()
        self.assertEqual((await self._wait_for(first, END)).payload["status"], "ok")


class SlotHandoffTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, slots: asyncio.Semaphore) -> RequestRun:
        return RequestRun(
            object(),  # type: ignore[arg-type]
            request_id="req-1",
            message="hi",
            session_id="s-1",
            request_metadata={},
            checkpoint={"trace_id": "trace-1"},
            slots=slots,
        )

    async def test_run_subagents_gives_the_slot_back_while_children_run(self) -> None:
        slots = asyncio.Semaphore(1)
        run = self._run(slots)
        await run._take_slot()
        seen: dict[str, bool] = {}

        async def _tool_call(*args: Any) -> dict:
            seen[args[-1]["name"]] = slots.locked()
            return {}

        with patch.object(engine_loop, "run_step_tool_call", _tool_call):
            for name in ("RunSubAgents", "work"):
                await run._tool_call(
                    {}, loop_index=0, call_index=0,
                    tool_call={"id": "c", "name": name, "arguments": {}},
                )

        self.assertEqual(seen, {"RunSubAgents": False, "work": True})
        self.assertTrue(slots.locked())

    async def test_a_cancel_while_taking_the_slot_back_cancels_the_request(self) -> None:
        slots = asyncio.Semaphore(1)
        run = self._run(slots)
        await run._take_slot()

        with self.assertRaises(RequestCancelled):
            async with run._slot_released():
                await slots.acquire()  # another request takes the free slot
                run.cancel()
        run._give_slot()
        slots.release()
        self.assertFalse(slots.locked())

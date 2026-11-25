"""A request's loop state rebuilt from its events.

The running loop keeps its state in memory; the event log is its only record.
These tests hold the rebuild to the running loop: at every step boundary the
state rebuilt from the events written so far equals the state the loop held,
and a recovered request runs only the work with no end event.
"""

from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.runtime import AgentRuntime, AgentSpec
from mash.runtime.engine import loop as loop_module
from mash.runtime.engine.loop import RequestRun
from mash.runtime.engine.rebuild import rebuild_request_state
from mash.runtime.events import RequestStatus, RuntimeEventType
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import TEST_IDENTITY
from mash.tools.ask_user import AskUserTool
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry


class _Tool:
    requires_approval = False
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self, name: str, *, parallel_safe: bool = True, block: bool = False) -> None:
        self.name = name
        self.description = f"The {name} tool."
        self.parallel_safe = parallel_safe
        self.runs = 0
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        self.runs += 1
        await self.release.wait()
        return ToolResult.success(f"{self.name}:{args.get('text', '')}")

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


def _call(call_id: str, name: str, text: str) -> tuple[ToolCall, LLMContentBlock]:
    arguments = {"text": text}
    return (
        ToolCall(id=call_id, name=name, arguments=arguments),
        LLMContentBlock.tool_call(tool_call_id=call_id, name=name, arguments=arguments),
    )


class _ScriptedProvider(LLMProvider):
    """Answers by the number of tool results the request holds so far."""

    def __init__(self, script: list[list[tuple[str, str, str]]], final: str) -> None:
        self._script = script
        self._final = final
        self.calls = 0

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        steps_done = sum(
            1 for message in request.messages if message.role == "assistant"
            and any(block.type == "tool_call" for block in message.content)
        )
        usage = LLMTokenUsage(input_tokens=10 + steps_done, output_tokens=2, total_tokens=12)
        thinking = LLMContentBlock(
            type="thinking", data={"thinking": f"step {steps_done}", "signature": f"sig-{steps_done}"}
        )
        if steps_done < len(self._script):
            pairs = [_call(*spec) for spec in self._script[steps_done]]
            return LLMResponse(
                thought_summary=f"step {steps_done}",
                text="",
                tool_calls=[pair[0] for pair in pairs],
                content_blocks=[thinking, *(pair[1] for pair in pairs)],
                stop_reason="tool_call",
                usage=usage,
            )
        return LLMResponse(
            thought_summary="done",
            text=self._final,
            tool_calls=[],
            content_blocks=[thinking, LLMContentBlock.text(self._final)],
            stop_reason="end_turn",
            usage=usage,
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class _Spec(AgentSpec):
    def __init__(self, provider: LLMProvider, tools: list[Any]) -> None:
        self.provider = provider
        self.tools = tools

    def get_agent_id(self) -> str:
        return "rebuild-app"

    def build_tools(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in self.tools:
            registry.register(tool)
        return registry

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(agent_id="rebuild-app", system_prompt="You are rebuild-app.")


# Fields the rebuild does not reproduce because nothing reads them back: the
# step clock (taken from chat.start rather than before it), and the parts of
# the model's usage other than input and output tokens.
_IGNORED = {"step_started_at"}


def _comparable(state: dict[str, Any]) -> dict[str, Any]:
    state = copy.deepcopy({k: v for k, v in state.items() if k not in _IGNORED})
    action = state.get("action")
    if isinstance(action, dict):
        action.pop("trace_id", None)
        usage = action.get("token_usage") or {}
        action["token_usage"] = {"input": usage.get("input"), "output": usage.get("output")}
    return state


class RebuildTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def _runtime(self, spec: AgentSpec) -> AgentRuntime:
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="s-1",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.engine.close)
        return runtime

    async def _wait_for(self, request_id: str, event_type: str, count: int = 1) -> list[Any]:
        for _ in range(500):
            events = [
                e
                for e in await self.runtime_store.list_request_events(request_id)
                if e.event_type == event_type
            ]
            if len(events) >= count:
                return events
            await asyncio.sleep(0.01)
        self.fail(f"{request_id} never reached {count} {event_type}")


class RebuildEquivalenceTests(RebuildTestCase):
    async def test_the_rebuild_matches_the_running_loop_at_every_boundary(self) -> None:
        provider = _ScriptedProvider(
            [
                # A parallel batch, then a serial call that runs alone.
                [("c1", "echo", "a"), ("c2", "echo", "b")],
                [("c3", "note", "c")],
            ],
            final="all done",
        )
        spec = _Spec(provider, [_Tool("echo"), _Tool("note", parallel_safe=False)])
        runtime = await self._runtime(spec)

        snapshots: list[tuple[str, dict[str, Any], int]] = []
        current: dict[str, str] = {}

        async def _snapshot(label: str, state: dict[str, Any]) -> dict[str, Any]:
            events = await self.runtime_store.list_request_events(current["request_id"])
            snapshots.append((label, copy.deepcopy(state), len(events)))
            return state

        def _wrap(label: str, fn: Any) -> Any:
            async def _wrapped(*args: Any, **kwargs: Any) -> Any:
                return await _snapshot(label, await fn(*args, **kwargs))

            return _wrapped

        act = RequestRun._act

        async def _act(run: RequestRun, state: dict[str, Any]) -> dict[str, Any]:
            return await _snapshot("act", await act(run, state))

        with patch.object(
            loop_module, "load_request_context", _wrap("load", loop_module.load_request_context)
        ), patch.object(
            loop_module, "plan_request_step", _wrap("plan", loop_module.plan_request_step)
        ), patch.object(
            loop_module, "commit_request_step", _wrap("commit", loop_module.commit_request_step)
        ), patch.object(RequestRun, "_act", _act):
            # The first event is written by the submit; the snapshots read the
            # request's events from then on.
            original_submit = runtime.submit_request

            accepted = await original_submit(message="go", session_id="s-1", **TEST_IDENTITY)
            current["request_id"] = accepted["request_id"]
            [end] = await self._wait_for(
                current["request_id"], RuntimeEventType.INVOKE_AGENT_END.value
            )
        self.assertEqual(end.payload["response"]["text"], "all done")

        labels = [label for label, _, _ in snapshots]
        self.assertEqual(
            labels,
            ["load", "plan", "act", "commit", "plan", "act", "commit", "plan", "act", "commit"],
        )
        events = await self.runtime_store.list_request_events(current["request_id"])
        for index, (label, live, count) in enumerate(snapshots):
            with self.subTest(boundary=f"{index}:{label}"):
                rebuilt = await rebuild_request_state(
                    runtime,
                    session_id="s-1",
                    message="go",
                    request_metadata=dict(
                        next(
                            e for e in events
                            if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value
                        ).payload["request_metadata"]
                    ),
                    events=events[:count],
                )
                self.assertEqual(_comparable(rebuilt), _comparable(live))

    async def test_loop_results_are_recorded_as_tool_events(self) -> None:
        from mash.testing.runtime_fixtures import ToolCallingAgentSpec

        spec = ToolCallingAgentSpec(
            agent_id="ask-app", tool_name="AskUser", arguments={"question": "Which one?"}
        )
        spec.build_tools = lambda: _registry(AskUserTool())  # type: ignore[method-assign]
        runtime = await self._runtime(spec)
        accepted = await runtime.submit_request(message="ask", session_id="s-1", **TEST_IDENTITY)
        request_id = accepted["request_id"]
        [opened] = await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)
        await runtime.answer_interaction(request_id, opened.payload["interaction_id"], "blue")
        await self._wait_for(request_id, RuntimeEventType.INVOKE_AGENT_END.value)

        [start] = await self._wait_for(request_id, RuntimeEventType.EXECUTE_TOOL_START.value)
        [tool_end] = await self._wait_for(request_id, RuntimeEventType.EXECUTE_TOOL_END.value)
        self.assertEqual(start.payload["attributes"]["gen_ai.tool.name"], "AskUser")
        self.assertEqual(tool_end.payload["result"]["content"], "blue")


def _registry(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


class RecoveryTests(RebuildTestCase):
    async def test_a_recovered_batch_reruns_only_the_unfinished_call(self) -> None:
        fast = _Tool("fast")
        slow = _Tool("slow", block=True)
        provider = _ScriptedProvider([[("c1", "fast", "x"), ("c2", "slow", "y")]], final="ok")
        spec = _Spec(provider, [fast, slow])
        first = await self._runtime(spec)
        accepted = await first.submit_request(message="go", session_id="s-1", **TEST_IDENTITY)
        request_id = accepted["request_id"]
        # fast has finished; slow is still running.
        await self._wait_for(request_id, RuntimeEventType.EXECUTE_TOOL_END.value)
        await first.engine.close()
        self.assertIs(
            await self.runtime_store.get_request_lifecycle(request_id), RequestStatus.RUNNING
        )

        slow.release.set()
        await self._runtime(spec)
        [end] = await self._wait_for(request_id, RuntimeEventType.INVOKE_AGENT_END.value)

        self.assertEqual(end.payload["status"], "ok")
        self.assertEqual(fast.runs, 1)
        self.assertEqual(slow.runs, 2)
        # The first model call has its chat.end; only the final answer is asked for.
        self.assertEqual(provider.calls, 2)
        ends = await self._wait_for(request_id, RuntimeEventType.EXECUTE_TOOL_END.value, 2)
        self.assertEqual(
            [e.payload["result"]["content"] for e in ends], ["fast:x", "slow:y"]
        )

    async def test_a_recovered_interaction_keeps_its_deadline(self) -> None:
        from mash.testing.runtime_fixtures import ToolCallingAgentSpec

        deadlines: list[float] = []
        await_answer = RequestRun._await_answer

        async def _recorded(run, interaction_id, *, deadline, interaction_type):
            deadlines.append(deadline)
            return await await_answer(
                run, interaction_id, deadline=deadline, interaction_type=interaction_type
            )

        patcher = patch.object(RequestRun, "_await_answer", _recorded)
        patcher.start()
        self.addCleanup(patcher.stop)

        spec = ToolCallingAgentSpec(
            agent_id="ask-app", tool_name="AskUser", arguments={"question": "Which one?"}
        )
        spec.build_tools = lambda: _registry(AskUserTool())  # type: ignore[method-assign]
        first = await self._runtime(spec)
        accepted = await first.submit_request(message="ask", session_id="s-1", **TEST_IDENTITY)
        request_id = accepted["request_id"]
        [opened] = await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)
        for _ in range(300):
            if deadlines:
                break
            await asyncio.sleep(0.01)
        await first.engine.close()

        await asyncio.sleep(0.05)
        second = await self._runtime(spec)
        for _ in range(300):
            if len(deadlines) >= 2:
                break
            await asyncio.sleep(0.01)
        # The same interaction, waited on to the same deadline.
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(deadlines[0], deadlines[1])
        starts = await self._wait_for(request_id, RuntimeEventType.INTERACTION_START.value)
        self.assertEqual(len(starts), 1)
        await second.answer_interaction(request_id, opened.payload["interaction_id"], "red")
        [end] = await self._wait_for(request_id, RuntimeEventType.INVOKE_AGENT_END.value)
        self.assertEqual(end.payload["response"]["text"], "red")


if __name__ == "__main__":
    unittest.main()

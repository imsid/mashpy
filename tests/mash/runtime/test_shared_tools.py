"""An agent's tools are built once per process and shared by every request."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.runtime import AgentRuntime
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry


class _CountingTool:
    name = "count"
    description = "Counts calls."
    requires_approval = False
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0
        self.shutdowns = 0

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        self.calls += 1
        return ToolResult.success(f"call {self.calls}")

    async def shutdown(self) -> None:
        self.shutdowns += 1

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class _ToolThenDoneLLM(LLMProvider):
    """Odd calls ask for the ``count`` tool, even calls finish."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        self.calls += 1
        usage = LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
        if self.calls % 2:
            call_id = f"call-{self.calls}"
            return LLMResponse(
                thought_summary="",
                text="",
                tool_calls=[ToolCall(id=call_id, name="count", arguments={})],
                content_blocks=[
                    LLMContentBlock.tool_call(tool_call_id=call_id, name="count", arguments={})
                ],
                stop_reason="tool_call",
                usage=usage,
            )
        return LLMResponse(
            thought_summary="",
            text="Done.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Done.")],
            stop_reason="end_turn",
            usage=usage,
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _Spec(AgentSpec):
    def __init__(self) -> None:
        self.tool = _CountingTool()
        self.build_tools_calls = 0
        self.llm = _ToolThenDoneLLM()

    def get_agent_id(self) -> str:
        return "shared-tools"

    def build_tools(self) -> ToolRegistry:
        self.build_tools_calls += 1
        tools = ToolRegistry()
        tools.register(self.tool)
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.llm

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="shared-tools", system_prompt="test")


class SharedToolsTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, runtime: AgentRuntime, session_id: str) -> str:
        accepted = await runtime.submit_request(
            message="go", session_id=session_id, user_id="u-1"
        )
        cursor, done, events = 0, False, []
        while not done:
            chunk, cursor, done = await runtime.stream_response_events(
                str(accepted["request_id"]), cursor=cursor, wait_timeout=1.0
            )
            events.extend(chunk)
        return events[-1]["event"]

    async def test_build_tools_runs_once_and_tools_shut_down_with_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                spec = _Spec()
                runtime_store, memory_store = build_test_stores()
                runtime = AgentRuntime.from_spec(
                    spec,
                    session_id="host-session",
                    runtime_store=runtime_store,
                    memory_store=memory_store,
                )
                await runtime.open()
                try:
                    self.assertEqual(await self._run(runtime, "s-1"), "request.completed")
                    self.assertEqual(await self._run(runtime, "s-2"), "request.completed")

                    self.assertEqual(spec.tool.calls, 2)
                    self.assertEqual(spec.llm.calls, 4)
                    self.assertEqual(spec.build_tools_calls, 1)
                    self.assertEqual(spec.tool.shutdowns, 0)
                finally:
                    await runtime.shutdown()
                self.assertEqual(spec.tool.shutdowns, 1)

    async def test_turn_agents_add_request_tools_without_touching_the_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                spec = _Spec()
                runtime_store, memory_store = build_test_stores()
                runtime = AgentRuntime.from_spec(
                    spec,
                    session_id="host-session",
                    runtime_store=runtime_store,
                    memory_store=memory_store,
                )
                try:
                    turn = runtime.build_turn_agent(session_id="s-1", trace_id="t-1", user_id="u-1")
                    self.assertIs(turn.tools.get("count"), spec.tool)
                    self.assertIs(turn.llm, runtime.agent.llm)
                    self.assertIn("search_conversations", turn.tools)
                    self.assertNotIn("search_conversations", runtime.agent.tools)
                    described = {tool["name"] for tool in runtime.describe()["tools"]}
                    self.assertTrue({"count", "search_conversations"} <= described)
                    self.assertEqual(spec.build_tools_calls, 1)
                finally:
                    await runtime.shutdown()


def test_registry_copy_shares_tools_but_not_registrations() -> None:
    tool = _CountingTool()
    base = ToolRegistry()
    base.register(tool)
    copy = base.copy()
    copy.unregister("count")
    assert "count" in base
    assert "count" not in copy
    again = base.copy()
    assert again.get("count") is tool

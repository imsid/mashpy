"""Tests for core agent loop behavior."""

from __future__ import annotations

import json
import time
import unittest
from typing import Optional

from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.context import Context, ToolCall
from mash.core.llm import BaseLLMProvider, LLMProvider
from mash.core.llm.types import (
    LLMContentBlock,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
)
from mash.memory.signals import build_default_signal_collector
from mash.skills.registry import SkillRegistry
from mash.tools.base import FunctionTool, ToolResult
from mash.tools.registry import ToolRegistry


class _LoopingLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            text="Let me inspect one more thing.",
            tool_calls=[ToolCall(id="call-1", name="noop", arguments={})],
            content_blocks=[
                LLMContentBlock.text("Let me inspect one more thing."),
                LLMContentBlock.tool_call(
                    tool_call_id="call-1",
                    name="noop",
                    arguments={},
                ),
            ],
            stop_reason="tool_call",
            usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _RecordingEventLogger:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _ToolThenFinishLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self._call_count = 0
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        self._call_count += 1
        if self._call_count == 1:
            return LLMResponse(
                text="I need one tool first.",
                tool_calls=[ToolCall(id="call-1", name="used_tool", arguments={})],
                content_blocks=[
                    LLMContentBlock.text("I need one tool first."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1",
                        name="used_tool",
                        arguments={},
                    ),
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            )

        return LLMResponse(
            text="Done.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Done.")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=3, output_tokens=1, total_tokens=4),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _FinishImmediatelyLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            text="Done immediately.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Done immediately.")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _LoggingFinishLLMProvider(BaseLLMProvider):
    provider_name = "test-provider"

    def __init__(self) -> None:
        super().__init__(app_id="test", model="test-model")

    async def send(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        await self._emit_request_start(request)
        response = LLMResponse(
            text="Done immediately.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Done immediately.")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )
        await self._emit_request_complete(
            request,
            started_at=started_at,
            response=response,
        )
        return response


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_returns_explicit_max_step_message_when_tool_loop_exhausts(
        self,
    ) -> None:
        async def noop(_args) -> ToolResult:
            return ToolResult.success("ok")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="noop",
                description="No-op tool",
                parameters={"type": "object", "properties": {}},
                _executor=noop,
            )
        )
        agent = Agent(
            llm=_LoopingLLMProvider(),
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=1,
            ),
        )
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        response = await agent.run(context)

        self.assertIn("max step limit", response.text)
        self.assertEqual(response.metadata["stop_reason"], "max_steps")

    async def test_tool_result_trace_preserves_structured_metadata(self) -> None:
        async def noop(_args) -> ToolResult:
            return ToolResult.success(
                "ok",
                subagent_session_id="subagent:research:abc123",
            )

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="noop",
                description="No-op tool",
                parameters={"type": "object", "properties": {}},
                _executor=noop,
            )
        )
        agent = Agent(
            llm=_LoopingLLMProvider(),
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=1,
            ),
        )
        logger = _RecordingEventLogger()
        agent.set_event_logger(logger, session_id="s-1")
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        await agent.run(context)

        result_events = [event for event in logger.events if event.event_type == "agent.tool.result"]
        self.assertEqual(len(result_events), 1)
        self.assertEqual(
            result_events[0].payload["metadata"]["subagent_session_id"],
            "subagent:research:abc123",
        )

    async def test_run_collects_unused_tool_signals_for_trace(self) -> None:
        provider = _ToolThenFinishLLMProvider()

        async def used_tool(_args) -> ToolResult:
            return ToolResult.success("used")

        async def unused_tool(_args) -> ToolResult:
            return ToolResult.success("unused")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="used_tool",
                description="Tool that will be called.",
                parameters={"type": "object", "properties": {}},
                _executor=used_tool,
            )
        )
        tools.register(
            FunctionTool(
                name="unused_tool",
                description="Tool that will remain unused.",
                parameters={"type": "object", "properties": {}},
                _executor=unused_tool,
            )
        )
        agent = Agent(
            llm=provider,
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=3,
            ),
        )
        agent.set_signal_collector(build_default_signal_collector())
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("use the tool")

        response = await agent.run(context)

        self.assertEqual(response.signals["unused_tools"], ["unused_tool"])
        first_request = provider.requests[0]
        unused_tool_def = next(
            tool for tool in first_request.tools if tool.name == "unused_tool"
        )
        base_estimate = int(len(json.dumps(unused_tool_def.to_debug_dict())) / 3.5)
        expected_tokens = int(base_estimate * 1.05)
        self.assertEqual(response.signals["unused_tool_tokens"], expected_tokens)

    async def test_run_collects_unused_tool_signals_when_trace_finishes_immediately(
        self,
    ) -> None:
        async def alpha_tool(_args) -> ToolResult:
            return ToolResult.success("alpha")

        async def beta_tool(_args) -> ToolResult:
            return ToolResult.success("beta")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="alpha_tool",
                description="Unused alpha tool.",
                parameters={"type": "object", "properties": {}},
                _executor=alpha_tool,
            )
        )
        tools.register(
            FunctionTool(
                name="beta_tool",
                description="Unused beta tool.",
                parameters={"type": "object", "properties": {}},
                _executor=beta_tool,
            )
        )
        agent = Agent(
            llm=_FinishImmediatelyLLMProvider(),
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=1,
            ),
        )
        agent.set_signal_collector(build_default_signal_collector())
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("just answer")

        response = await agent.run(context)

        self.assertEqual(response.signals["unused_tools"], ["alpha_tool", "beta_tool"])
        self.assertGreater(int(response.signals["unused_tool_tokens"]), 0)

    async def test_run_emits_llm_and_agent_trace_events_without_removed_debug_events(
        self,
    ) -> None:
        agent = Agent(
            llm=_LoggingFinishLLMProvider(),
            tools=ToolRegistry(),
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=1,
            ),
        )
        logger = _RecordingEventLogger()
        agent.set_event_logger(logger, session_id="s-1")
        agent.llm.set_event_logger(logger, session_id="s-1", app_id="test")
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("just answer")

        await agent.run(context)

        event_types = [event.event_type for event in logger.events]
        self.assertIn("llm.request.start", event_types)
        self.assertIn("llm.request.complete", event_types)
        self.assertIn("agent.think.complete", event_types)
        self.assertNotIn("agent.prompt.token_breakdown", event_types)
        self.assertNotIn("agent.tools.token_breakdown", event_types)
        self.assertNotIn("agent.llm.response", event_types)


if __name__ == "__main__":
    unittest.main()

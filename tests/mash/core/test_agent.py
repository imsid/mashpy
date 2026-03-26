"""Tests for core agent loop behavior."""

from __future__ import annotations

import json
import unittest
from typing import Optional

from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.context import Context, ToolCall
from mash.core.llm import LLMProvider
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

    def send(self, request: LLMRequest) -> LLMResponse:
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

    def emit(self, event) -> None:
        self.events.append(event)


class _ToolThenFinishLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self._call_count = 0
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "test-model"

    def send(self, request: LLMRequest) -> LLMResponse:
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

    def send(self, request: LLMRequest) -> LLMResponse:
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


class AgentLoopTests(unittest.TestCase):
    def test_run_returns_explicit_max_step_message_when_tool_loop_exhausts(self) -> None:
        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="noop",
                description="No-op tool",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success("ok"),
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

        response = agent.run(context)

        self.assertIn("max step limit", response.text)
        self.assertEqual(response.metadata["stop_reason"], "max_steps")

    def test_tool_result_trace_preserves_structured_metadata(self) -> None:
        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="noop",
                description="No-op tool",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success(
                    "ok",
                    subagent_session_id="subagent:research:abc123",
                ),
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

        agent.run(context)

        result_events = [event for event in logger.events if event.event_type == "agent.tool.result"]
        self.assertEqual(len(result_events), 1)
        self.assertEqual(
            result_events[0].payload["metadata"]["subagent_session_id"],
            "subagent:research:abc123",
        )

    def test_run_collects_unused_tool_signals_for_trace(self) -> None:
        provider = _ToolThenFinishLLMProvider()
        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="used_tool",
                description="Tool that will be called.",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success("used"),
            )
        )
        tools.register(
            FunctionTool(
                name="unused_tool",
                description="Tool that will remain unused.",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success("unused"),
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

        response = agent.run(context)

        self.assertEqual(response.signals["unused_tools"], ["unused_tool"])
        first_request = provider.requests[0]
        unused_tool_def = next(
            tool for tool in first_request.tools if tool.name == "unused_tool"
        )
        base_estimate = int(len(json.dumps(unused_tool_def.to_debug_dict())) / 3.5)
        expected_tokens = int(base_estimate * 1.05)
        self.assertEqual(response.signals["unused_tool_tokens"], expected_tokens)

    def test_run_collects_unused_tool_signals_when_trace_finishes_immediately(self) -> None:
        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="alpha_tool",
                description="Unused alpha tool.",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success("alpha"),
            )
        )
        tools.register(
            FunctionTool(
                name="beta_tool",
                description="Unused beta tool.",
                parameters={"type": "object", "properties": {}},
                _executor=lambda args: ToolResult.success("beta"),
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

        response = agent.run(context)

        self.assertEqual(response.signals["unused_tools"], ["alpha_tool", "beta_tool"])
        self.assertGreater(int(response.signals["unused_tool_tokens"]), 0)


if __name__ == "__main__":
    unittest.main()

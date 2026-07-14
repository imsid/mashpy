"""Tests for core agent loop behavior."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from typing import Optional

from mash.core.agent import Agent, TruncatedToolCallError
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
from mash.runtime.errors import classify_error
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


class _ResponseThenFinishLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self._call_count = 0

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        self._call_count += 1
        if self._call_count == 1:
            return LLMResponse(
                text="Partial response.",
                tool_calls=[],
                content_blocks=[LLMContentBlock.text("Partial response.")],
                stop_reason="pause_turn",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
        return LLMResponse(
            text="Final answer.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Final answer.")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _MaxTokensLLMProvider(LLMProvider):
    """Always truncates a text response on max_tokens.

    Records the role of the last message in every request so tests can assert
    the loop never builds an assistant-terminated (prefill) follow-up request.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_message_roles: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if request.messages:
            self.last_message_roles.append(request.messages[-1].role)
        return LLMResponse(
            text="A very long truncated answer",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("A very long truncated answer")],
            stop_reason="max_tokens",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _MaxTokensToolCallLLMProvider(LLMProvider):
    """Truncates on max_tokens while emitting a tool call.

    Mirrors a model that ran out of budget mid-tool-call: the call's arguments
    are incomplete and must be dropped rather than executed.
    """

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        self.call_count += 1
        return LLMResponse(
            text="Let me look that up",
            tool_calls=[ToolCall(id="call-1", name="search", arguments={})],
            content_blocks=[
                LLMContentBlock.text("Let me look that up"),
                LLMContentBlock.tool_call(
                    tool_call_id="call-1",
                    name="search",
                    arguments={},
                ),
            ],
            stop_reason="max_tokens",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _ToolUseInvalidThenFinishLLMProvider(LLMProvider):
    """Emits a stop_reason="tool_use" response whose tool call fails validation.

    The call omits a required argument, so it is surfaced to the model as a tool
    error rather than executed. On the follow-up turn the model finishes.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_message_roles: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if request.messages:
            self.last_message_roles.append(request.messages[-1].role)
        if self.call_count == 1:
            return LLMResponse(
                text="Saving that now.",
                tool_calls=[ToolCall(id="call-1", name="save", arguments={})],
                content_blocks=[
                    LLMContentBlock.text("Saving that now."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1",
                        name="save",
                        arguments={},
                    ),
                ],
                stop_reason="tool_use",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
        return LLMResponse(
            text="All done.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("All done.")],
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

    async def test_run_continues_after_non_terminal_response(self) -> None:
        agent = Agent(
            llm=_ResponseThenFinishLLMProvider(),
            tools=ToolRegistry(),
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=3,
            ),
        )
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("keep going")

        response = await agent.run(context)

        self.assertEqual(response.text, "Final answer.")
        assistant_messages = [
            msg for msg in response.context.messages if msg.role.value == "assistant"
        ]
        self.assertEqual(len(assistant_messages), 2)

    async def test_run_terminates_on_max_tokens_without_assistant_prefill(
        self,
    ) -> None:
        # A max_tokens stop on a text response must end the turn, not loop into
        # a follow-up request that ends with an assistant message (a prefill the
        # provider would reject with a 400).
        llm = _MaxTokensLLMProvider()
        agent = Agent(
            llm=llm,
            tools=ToolRegistry(),
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=5,
            ),
        )
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("write a long report")

        response = await agent.run(context)

        # The loop stopped after a single generation.
        self.assertEqual(llm.call_count, 1)
        # No request was ever built with a trailing assistant (prefill) message.
        self.assertTrue(llm.last_message_roles)
        self.assertNotIn("assistant", llm.last_message_roles)
        # The partial answer is returned and the truncation is observable.
        self.assertEqual(response.text, "A very long truncated answer")
        self.assertTrue(response.metadata.get("truncated"))
        self.assertEqual(response.metadata.get("stop_reason"), "max_tokens")
        # A plain text truncation is not a dropped tool call.
        self.assertIsNone(response.metadata.get("truncated_tool_call"))

    async def test_run_raises_on_truncated_tool_call_on_max_tokens(self) -> None:
        # A max_tokens stop mid-tool-call must not execute the incomplete call
        # nor finish the turn as completed — the model's intended action was
        # dropped, so the turn surfaces a terminal error instead.
        async def search(_args) -> ToolResult:
            raise AssertionError("truncated tool call must not execute")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="search",
                description="Search tool",
                parameters={"type": "object", "properties": {}},
                _executor=search,
            )
        )
        llm = _MaxTokensToolCallLLMProvider()
        agent = Agent(
            llm=llm,
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=5,
            ),
        )
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("look something up")

        with self.assertRaises(TruncatedToolCallError):
            await agent.run(context)

        # The loop stopped after one generation; the tool was never called.
        self.assertEqual(llm.call_count, 1)

    async def test_truncated_tool_call_error_is_non_retryable(self) -> None:
        # The runtime must treat the truncation as terminal, not retry it: an
        # identical re-request re-truncates. classify_error keys off the
        # exception's explicit retryable/error_code attributes.
        classified = classify_error(TruncatedToolCallError("boom"))
        self.assertFalse(classified["retryable"])
        self.assertEqual(classified["error_code"], "truncated_tool_call")

    async def test_run_surfaces_invalid_tool_call_without_assistant_prefill(
        self,
    ) -> None:
        # A stop_reason="tool_use" response whose only tool call fails validation
        # must not drop the call and continue into an assistant-terminated
        # (prefill) request. The failure is surfaced to the model as a tool error
        # and logged; the model then finishes.
        async def save(_args) -> ToolResult:
            raise AssertionError("invalid tool call must not execute")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="save",
                description="Persist data",
                parameters={
                    "type": "object",
                    "properties": {"data": {"type": "string"}},
                    "required": ["data"],
                },
                _executor=save,
            )
        )
        llm = _ToolUseInvalidThenFinishLLMProvider()
        logger = _RecordingEventLogger()
        agent = Agent(
            llm=llm,
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=5,
            ),
        )
        agent.set_event_logger(logger, "s-1")
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("save the record")

        response = await agent.run(context)

        # The invalid call was fed back as an error and the model corrected,
        # finishing the turn.
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(response.text, "All done.")
        # No request was ever built ending with an assistant (prefill) message.
        self.assertTrue(llm.last_message_roles)
        self.assertNotIn("assistant", llm.last_message_roles)
        # The follow-up request ended with the tool-result message.
        self.assertEqual(llm.last_message_roles[-1], "tool")
        # The invalidation was logged, naming the missing argument.
        invalid_events = [
            event
            for event in logger.events
            if getattr(event, "event_type", None) == "agent.tool.invalid"
        ]
        self.assertEqual(len(invalid_events), 1)
        self.assertEqual(invalid_events[0].payload["missing_arguments"], ["data"])

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


def _build_agent(tools: ToolRegistry, **config_kwargs) -> Agent:
    return Agent(
        llm=_FinishImmediatelyLLMProvider(),
        tools=tools,
        skills=SkillRegistry(),
        config=AgentConfig(
            app_id="test",
            system_prompt="You are a test agent.",
            **config_kwargs,
        ),
    )


def _tool_call(name: str, call_id: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ParallelToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_safe_calls_run_concurrently(self) -> None:
        order: list[str] = []

        def make_tool(name: str, delay: float) -> FunctionTool:
            async def run(_args) -> ToolResult:
                order.append(f"start:{name}")
                await asyncio.sleep(delay)
                order.append(f"end:{name}")
                return ToolResult.success(name)

            return FunctionTool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                _executor=run,
            )

        tools = ToolRegistry()
        tools.register(make_tool("slow", 0.05))
        tools.register(make_tool("fast", 0.0))
        agent = _build_agent(tools)

        start = time.monotonic()
        results = await agent._execute_tool_calls(
            [_tool_call("slow", "c1"), _tool_call("fast", "c2")]
        )
        elapsed = time.monotonic() - start

        # fast finishes before slow despite being scheduled second -> concurrent.
        self.assertEqual(order[0], "start:slow")
        self.assertEqual(order[1], "start:fast")
        self.assertIn("end:fast", order[: order.index("end:slow")])
        self.assertLess(elapsed, 0.05 * 1.8)
        # Results stay in call order, mapped to their tool_call_ids.
        self.assertEqual([r.tool_call_id for r in results], ["c1", "c2"])

    async def test_one_failure_does_not_abort_the_batch(self) -> None:
        async def boom(_args) -> ToolResult:
            raise RuntimeError("kaboom")

        async def ok(_args) -> ToolResult:
            return ToolResult.success("fine")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="boom",
                description="raises",
                parameters={"type": "object", "properties": {}},
                _executor=boom,
            )
        )
        tools.register(
            FunctionTool(
                name="ok",
                description="succeeds",
                parameters={"type": "object", "properties": {}},
                _executor=ok,
            )
        )
        agent = _build_agent(tools)

        results = await agent._execute_tool_calls(
            [_tool_call("boom", "c1"), _tool_call("ok", "c2")]
        )

        by_id = {r.tool_call_id: r for r in results}
        self.assertTrue(by_id["c1"].is_error)
        self.assertIn("kaboom", by_id["c1"].content)
        self.assertFalse(by_id["c2"].is_error)
        self.assertEqual(by_id["c2"].content, "fine")

    async def test_unsafe_tool_acts_as_serial_barrier(self) -> None:
        order: list[str] = []

        def make_tool(name: str, *, parallel_safe: bool) -> FunctionTool:
            async def run(_args) -> ToolResult:
                order.append(f"start:{name}")
                await asyncio.sleep(0.01)
                order.append(f"end:{name}")
                return ToolResult.success(name)

            return FunctionTool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                _executor=run,
                parallel_safe=parallel_safe,
            )

        tools = ToolRegistry()
        tools.register(make_tool("a", parallel_safe=True))
        tools.register(make_tool("serial", parallel_safe=False))
        tools.register(make_tool("b", parallel_safe=True))
        agent = _build_agent(tools)

        results = await agent._execute_tool_calls(
            [
                _tool_call("a", "c1"),
                _tool_call("serial", "c2"),
                _tool_call("b", "c3"),
            ]
        )

        # Nothing crosses the serial barrier: a completes, then serial runs
        # alone, then b runs.
        self.assertEqual(
            order,
            [
                "start:a",
                "end:a",
                "start:serial",
                "end:serial",
                "start:b",
                "end:b",
            ],
        )
        self.assertEqual([r.tool_call_id for r in results], ["c1", "c2", "c3"])


class _PhasedLLMProvider(LLMProvider):
    """Returns a response with phase and reasoning_tokens for testing propagation."""

    def __init__(self, phase: Optional[str] = None, reasoning_tokens: Optional[int] = None) -> None:
        self._phase = phase
        self._reasoning_tokens = reasoning_tokens

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        provider_metadata = {}
        if self._phase:
            provider_metadata["phase"] = self._phase
        usage = LLMTokenUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            reasoning_tokens=self._reasoning_tokens,
        )
        return LLMResponse(
            text="I'll call the tool.",
            tool_calls=[ToolCall(id="call-1", name="noop", arguments={})],
            content_blocks=[
                LLMContentBlock.text("I'll call the tool."),
                LLMContentBlock.tool_call(
                    tool_call_id="call-1", name="noop", arguments={}
                ),
            ],
            stop_reason="tool_call",
            usage=usage,
            provider_metadata=provider_metadata,
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class AgentReasoningPropagationTests(unittest.IsolatedAsyncioTestCase):
    """Tests for phase and reasoning_tokens propagation added in #94."""

    def _build_agent(self, llm: LLMProvider) -> Agent:
        async def noop(_args) -> ToolResult:
            return ToolResult.success("ok")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="noop",
                description="no-op",
                parameters={"type": "object", "properties": {}},
                _executor=noop,
            )
        )
        return Agent(
            llm=llm,
            tools=tools,
            skills=SkillRegistry(),
            config=AgentConfig(
                app_id="test",
                system_prompt="You are a test agent.",
                max_steps=2,
            ),
        )

    async def test_phase_from_provider_metadata_stored_in_context_message(
        self,
    ) -> None:
        agent = self._build_agent(_PhasedLLMProvider(phase="commentary"))
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        await agent.plan_step(context)

        assistant_messages = [
            msg for msg in context.messages if msg.role.value == "assistant"
        ]
        self.assertTrue(assistant_messages)
        self.assertEqual(assistant_messages[-1].metadata.get("phase"), "commentary")

    async def test_no_phase_metadata_when_provider_metadata_has_no_phase(
        self,
    ) -> None:
        agent = self._build_agent(_PhasedLLMProvider(phase=None))
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        await agent.plan_step(context)

        assistant_messages = [
            msg for msg in context.messages if msg.role.value == "assistant"
        ]
        self.assertNotIn("phase", assistant_messages[-1].metadata)

    async def test_reasoning_tokens_included_in_step_token_usage(self) -> None:
        agent = self._build_agent(_PhasedLLMProvider(reasoning_tokens=380))
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        plan = await agent.plan_step(context)

        self.assertEqual(plan.token_usage.get("reasoning"), 380)

    async def test_reasoning_tokens_absent_from_token_usage_when_none(self) -> None:
        agent = self._build_agent(_PhasedLLMProvider(reasoning_tokens=None))
        context = Context(system_prompt="You are a test agent.")
        context.add_user_message("do the thing")

        plan = await agent.plan_step(context)

        self.assertNotIn("reasoning", plan.token_usage)


if __name__ == "__main__":
    unittest.main()

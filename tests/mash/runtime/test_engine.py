"""Tests for composed runtime engine behavior."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

from pydantic import BaseModel

from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import BaseLLMProvider, LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.mcp.types import MCPServerConfig
from conftest import build_test_stores
from mash.runtime import AgentRuntime
from mash.runtime.events import RuntimeEventType, build_reasoning_trace
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import build_spec
from mash.tools.base import FunctionTool, ToolResult
from mash.tools.registry import ToolRegistry
from mash.tools.web_search import ParallelSearchProvider


def _test_stores() -> dict:
    """Return runtime_store and memory_store kwargs for AgentRuntime.from_spec."""
    rs, ms = build_test_stores()
    return {"runtime_store": rs, "memory_store": ms}


class _FakeLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self.last_session_id: str | None = None
        self.last_app_id: str | None = None

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger
        self.last_session_id = session_id
        self.last_app_id = app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


class _BaseDefinition(AgentSpec):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        self.root = root
        self.app_id = app_id
        self.startup_called = False
        self.shutdown_called = False

    def get_agent_id(self) -> str:
        return self.app_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.app_id, system_prompt="You are a test app.")

    def on_startup(self, runtime: AgentRuntime) -> None:
        del runtime
        self.startup_called = True

    def on_shutdown(self, runtime: AgentRuntime) -> None:
        del runtime
        self.shutdown_called = True


class _ToolSignalsLLMProvider(LLMProvider):
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
                text="Need one tool.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="used_tool",
                        arguments={},
                    )
                ],
                content_blocks=[
                    LLMContentBlock.text("Need one tool."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1",
                        name="used_tool",
                        arguments={},
                    ),
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
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


class _AlwaysRespondLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(
            text="Still responding.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Still responding.")],
            stop_reason="pause_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _StructuredOutputLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        structured_output = request.provider_options.get("structured_output")
        if structured_output:
            return LLMResponse(
                text='{"title":"Changelog","commits_scanned":5}',
                tool_calls=[],
                content_blocks=[
                    LLMContentBlock.text('{"title":"Changelog","commits_scanned":5}')
                ],
                stop_reason="end_turn",
                usage=LLMTokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            )
        return LLMResponse(
            text="I scanned the recent commits.",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("I scanned the recent commits.")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _SignalsDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _ToolSignalsLLMProvider()

    def build_tools(self) -> ToolRegistry:
        async def used_tool(_args: Dict[str, Any]) -> ToolResult:
            return ToolResult.success("used")

        async def unused_tool(_args: Dict[str, Any]) -> ToolResult:
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
        return tools

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a test app.",
            max_steps=3,
        )

    def enable_runtime_tools(self) -> bool:
        return False


class _ResponseThenFinishDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _ResponseThenFinishLLMProvider()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a test app.",
            max_steps=3,
        )


class _TwoToolCallLLMProvider(LLMProvider):
    """Emits two tool calls in one turn, then finishes."""

    def __init__(self, names: tuple[str, str]) -> None:
        self._names = names
        self._call_count = 0

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        self._call_count += 1
        if self._call_count == 1:
            a, b = self._names
            return LLMResponse(
                text="Two tools.",
                tool_calls=[
                    ToolCall(id="call-a", name=a, arguments={}),
                    ToolCall(id="call-b", name=b, arguments={}),
                ],
                content_blocks=[
                    LLMContentBlock.text("Two tools."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-a", name=a, arguments={}
                    ),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-b", name=b, arguments={}
                    ),
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
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


class _ParallelToolsDefinition(_BaseDefinition):
    """Two tools that each sleep and record their wall-clock interval.

    Concurrency is detected by interval overlap rather than shared asyncio
    primitives, which would not survive the runtime executing the workflow on a
    different event loop than the test's. ``intervals`` maps tool name to its
    (start, end) monotonic times.
    """

    def __init__(
        self,
        root: Path,
        *,
        app_id: str = "test-app",
        second_parallel_safe: bool = True,
        sleep_seconds: float = 0.25,
    ) -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _TwoToolCallLLMProvider(("tool_a", "tool_b"))
        self.intervals: dict[str, tuple[float, float]] = {}
        self._second_parallel_safe = second_parallel_safe
        self._sleep_seconds = sleep_seconds

    def _overlap(self) -> bool:
        if len(self.intervals) < 2:
            return False
        (s_a, e_a), (s_b, e_b) = self.intervals["a"], self.intervals["b"]
        return s_a < e_b and s_b < e_a

    def build_tools(self) -> ToolRegistry:
        async def run(name: str) -> ToolResult:
            start = time.monotonic()
            await asyncio.sleep(self._sleep_seconds)
            self.intervals[name] = (start, time.monotonic())
            return ToolResult.success(name)

        async def tool_a(_args: Dict[str, Any]) -> ToolResult:
            return await run("a")

        async def tool_b(_args: Dict[str, Any]) -> ToolResult:
            return await run("b")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="tool_a",
                description="First tool.",
                parameters={"type": "object", "properties": {}},
                _executor=tool_a,
            )
        )
        tools.register(
            FunctionTool(
                name="tool_b",
                description="Second tool.",
                parameters={"type": "object", "properties": {}},
                _executor=tool_b,
                parallel_safe=self._second_parallel_safe,
            )
        )
        return tools

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a test app.",
            max_steps=3,
        )

    def enable_runtime_tools(self) -> bool:
        return False


class _ConcurrencyCapDefinition(_BaseDefinition):
    """Registers several tools that record peak simultaneous execution."""

    def __init__(
        self,
        root: Path,
        *,
        app_id: str = "test-app",
        tool_count: int = 4,
        max_parallel_tools: int = 2,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(root, app_id=app_id)
        self.tool_count = tool_count
        self.max_parallel_tools = max_parallel_tools
        self._sleep_seconds = sleep_seconds
        self.active = 0
        self.peak = 0

    def build_tools(self) -> ToolRegistry:
        async def run(_args: Dict[str, Any]) -> ToolResult:
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(self._sleep_seconds)
            finally:
                self.active -= 1
            return ToolResult.success("ok")

        tools = ToolRegistry()
        for i in range(self.tool_count):
            tools.register(
                FunctionTool(
                    name=f"tool_{i}",
                    description=f"Tool {i}.",
                    parameters={"type": "object", "properties": {}},
                    _executor=run,
                )
            )
        return tools

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a test app.",
            max_steps=3,
            max_parallel_tools=self.max_parallel_tools,
        )

    def enable_runtime_tools(self) -> bool:
        return False


class _AlwaysRespondDefinition(_BaseDefinition):
    def build_llm(self) -> LLMProvider:
        return _AlwaysRespondLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a test app.",
            max_steps=2,
        )


class _StructuredOutputDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _StructuredOutputLLMProvider()

    def build_llm(self) -> LLMProvider:
        return self.provider


class _FakeMCPClient:
    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "Search remote data.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

    def close(self) -> None:
        return None


class _MCPDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _MCPAssertingLLMProvider()

    def build_mcp_servers(self) -> list[MCPServerConfig]:
        return [
            MCPServerConfig(
                name="github",
                url="https://example.test/mcp",
            )
        ]

    def build_llm(self) -> LLMProvider:
        return self.provider


class _FakeWebSearchClient:
    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "web_search",
                "description": "Search the web.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "web_fetch",
                "description": "Fetch a URL.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def close(self) -> None:
        return None


class _WebSearchAssertingLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self.call_count = 0
        self.last_tool_names: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.last_tool_names = [tool.name for tool in request.tools]
        self.call_count += 1
        return LLMResponse(
            text="ok",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("ok")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _WebSearchDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.provider = _WebSearchAssertingLLMProvider()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def enable_runtime_tools(self) -> bool:
        return False

    def build_web_search(self):
        return ParallelSearchProvider(api_key="test")


class _MismatchedDefinition(_BaseDefinition):
    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="different-app-id", system_prompt="Mismatch")


class _CompactionLoggingLLMProvider(BaseLLMProvider):
    provider_name = "test-provider"

    def __init__(self, *, app_id: str) -> None:
        super().__init__(app_id=app_id, model="test-model")
        self.trace_ids_set: list[str | None] = []
        self.trace_id_during_send: str | None = None

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        self.trace_ids_set.append(trace_id)
        super().set_trace_id(trace_id)

    async def send(self, request: LLMRequest) -> LLMResponse:
        started_at = time.time()
        self.trace_id_during_send = self._trace_id
        await self._emit_request_start(request)
        response = LLMResponse(
            text="Summary:\n- compacted",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("Summary:\n- compacted")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )
        await self._emit_request_complete(
            request,
            started_at=started_at,
            response=response,
        )
        return response


class _CompactionLoggingDefinition(_BaseDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        super().__init__(root, app_id=app_id)
        self.last_built_llm: _CompactionLoggingLLMProvider | None = None

    def build_llm(self) -> LLMProvider:
        llm = _CompactionLoggingLLMProvider(app_id=self.app_id)
        self.last_built_llm = llm
        return llm


class _SessionBindingLLMProvider(LLMProvider):
    def __init__(self, runtime_provider, *, expected_session_id: str) -> None:
        self._runtime_provider = runtime_provider
        self._expected_session_id = expected_session_id
        self.last_session_id: str | None = None
        self.asserted = False

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        runtime = self._runtime_provider()
        if runtime is None:
            raise AssertionError("runtime binding is required")
        if self.last_session_id != self._expected_session_id:
            raise AssertionError(
                f"expected llm session {self._expected_session_id}, got {self.last_session_id}"
            )
        self.asserted = True
        return LLMResponse(
            text="hello",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("hello")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, app_id
        self.last_session_id = session_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


class _SessionBindingDefinition(_BaseDefinition):
    def __init__(
        self,
        root: Path,
        *,
        expected_session_id: str,
        app_id: str = "test-app",
    ) -> None:
        super().__init__(root, app_id=app_id)
        self.runtime: AgentRuntime | None = None
        self.provider = _SessionBindingLLMProvider(
            lambda: self.runtime,
            expected_session_id=expected_session_id,
        )

    def build_llm(self) -> LLMProvider:
        return self.provider


class _MCPAssertingLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self.call_count = 0
        self.last_session_id: str | None = None

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        tool_names = [tool.name for tool in request.tools]
        if "mcp_github_search" not in tool_names:
            raise AssertionError("remote MCP tool should be present in request tools")
        self.call_count += 1
        return LLMResponse(
            text="ok",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("ok")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, app_id
        self.last_session_id = session_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._memory_env = patch.dict(
            os.environ,
            {"MASH_DATABASE_URL": ""},
        )
        self._memory_env.start()
        self.addCleanup(self._memory_env.stop)
        self._runtime_database = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        self._runtime_database.start()
        self.addCleanup(self._runtime_database.stop)

    async def _collect_request_events(
        self,
        runtime: AgentRuntime,
        request_id: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        done = False
        while not done:
            chunk, cursor, done = await runtime.stream_response_events(
                request_id,
                cursor=cursor,
                wait_timeout=1.0,
            )
            events.extend(chunk)
        return events

    async def _invoke_request(
        self,
        runtime: AgentRuntime,
        *,
        message: str,
        session_id: str | None = None,
        structured_output: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        target_session_id = session_id or runtime.session_id
        accepted = await runtime.submit_request(
            message=message,
            session_id=target_session_id,
            structured_output=structured_output,
        )
        events = await self._collect_request_events(
            runtime, str(accepted["request_id"])
        )
        terminal = events[-1]
        self.assertEqual(terminal["event"], "request.completed")
        return accepted, events, dict(terminal["data"])

    async def test_boots_and_shutdown_without_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _BaseDefinition(Path(tmp))
                runtime = AgentRuntime.from_spec(definition, session_id="host-session", **_test_stores())
                self.assertTrue(definition.startup_called)
                self.assertFalse(hasattr(runtime, "renderer"))
                await runtime.shutdown()
                self.assertTrue(definition.shutdown_called)

    def test_app_id_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with self.assertRaises(ValueError):
                    AgentRuntime.from_spec(
                        _MismatchedDefinition(Path(tmp)),
                        session_id="host-session",
                        **_test_stores(),
                    )

    def test_runtime_package_does_not_export_runtime_turn_result(self) -> None:
        import mash.runtime as runtime_pkg

        self.assertFalse(hasattr(runtime_pkg, "RuntimeTurnResult"))

    async def test_submit_request_saves_turn_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(agent_id="test-app", response_text="hello"),
                    session_id="host-session",
                    **_test_stores(),
                )
                await runtime.open()
                _, _, result = await self._invoke_request(runtime, message="hi")

                self.assertEqual(result["session_total_tokens"], 3)
                turns = await runtime.store.get_turns(
                    session_id=runtime.session_id,
                    app_id=runtime.app_id,
                    limit=1,
                )
                self.assertEqual(turns[-1]["user_message"], "hi")
                self.assertEqual(turns[-1]["agent_response"], "hello")
                self.assertEqual(turns[-1]["metadata"]["token_usage"]["input"], 2)

                await runtime.shutdown()

    async def test_tool_batch_step_runs_calls_concurrently_in_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _ParallelToolsDefinition(Path(tmp))
                runtime = AgentRuntime.from_spec(
                    definition, session_id="host-session", **_test_stores()
                )
                await runtime.open()

                # Drive the batch step directly on the test loop. (Routing a
                # full request through DBOS under IsolatedAsyncioTestCase runs
                # the gathered step on a loop the harness does not pump
                # concurrently; the production server loop does — verified out
                # of band. Calling the step here keeps the concurrency check on
                # one loop and deterministic.)
                from mash.runtime.engine.steps import run_step_tool_batch

                new_state = await run_step_tool_batch(
                    definition.app_id,
                    "req-batch",
                    runtime.session_id,
                    "trace-batch",
                    {"loop_index": 0, "result_payloads": [], "tool_usage": {}},
                    [
                        {"id": "call-a", "name": "tool_a", "arguments": {}},
                        {"id": "call-b", "name": "tool_b", "arguments": {}},
                    ],
                )

                # Both calls ran with overlapping windows (concurrent), and
                # results are appended in call order.
                self.assertEqual(set(definition.intervals), {"a", "b"})
                self.assertTrue(definition._overlap())
                self.assertEqual(
                    [rp["tool_call_id"] for rp in new_state["result_payloads"]],
                    ["call-a", "call-b"],
                )

                await runtime.shutdown()

    async def test_tool_batch_step_caps_concurrency_at_max_parallel_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                from mash.runtime.engine.steps import run_step_tool_batch

                definition = _ConcurrencyCapDefinition(
                    Path(tmp), tool_count=4, max_parallel_tools=2
                )
                runtime = AgentRuntime.from_spec(
                    definition, session_id="host-session", **_test_stores()
                )
                await runtime.open()

                new_state = await run_step_tool_batch(
                    definition.app_id,
                    "req-cap",
                    runtime.session_id,
                    "trace-cap",
                    {"loop_index": 0, "result_payloads": [], "tool_usage": {}},
                    [
                        {"id": f"call-{i}", "name": f"tool_{i}", "arguments": {}}
                        for i in range(4)
                    ],
                )

                # All four ran, but never more than max_parallel_tools at once.
                self.assertEqual(len(new_state["result_payloads"]), 4)
                self.assertEqual(definition.peak, 2)
                self.assertEqual(
                    [rp["tool_call_id"] for rp in new_state["result_payloads"]],
                    ["call-0", "call-1", "call-2", "call-3"],
                )

                await runtime.shutdown()

    async def test_submit_request_serializes_non_parallel_safe_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                # tool_b opts out of parallelism, so it acts as a barrier and
                # cannot overlap tool_a.
                definition = _ParallelToolsDefinition(
                    Path(tmp), second_parallel_safe=False
                )
                runtime = AgentRuntime.from_spec(
                    definition, session_id="host-session", **_test_stores()
                )
                await runtime.open()
                await self._invoke_request(runtime, message="go")

                # Both still run, but serially: their windows do not overlap.
                self.assertEqual(set(definition.intervals), {"a", "b"})
                self.assertFalse(definition._overlap())

                await runtime.shutdown()

    async def test_submit_request_attaches_structured_output_after_completion(self) -> None:
        class ChangelogOutput(BaseModel):
            title: str
            commits_scanned: int

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _StructuredOutputDefinition(Path(tmp))
                runtime = AgentRuntime.from_spec(definition, session_id="host-session", **_test_stores())
                await runtime.open()
                _, _, result = await self._invoke_request(
                    runtime,
                    message="make a changelog",
                    structured_output=ChangelogOutput,
                )

                self.assertEqual(
                    result["response"]["text"],
                    "I scanned the recent commits.",
                )
                self.assertEqual(
                    result["response"]["structured_output"],
                    {"title": "Changelog", "commits_scanned": 5},
                )
                self.assertEqual(len(definition.provider.requests), 2)
                finalizer_request = definition.provider.requests[-1]
                self.assertEqual(finalizer_request.tools, [])
                self.assertEqual(
                    finalizer_request.provider_options["structured_output"]["title"],
                    "ChangelogOutput",
                )
                self.assertEqual(
                    finalizer_request.provider_options["structured_output"]["type"],
                    "object",
                )
                self.assertFalse(
                    finalizer_request.provider_options["structured_output"][
                        "additionalProperties"
                    ]
                )
                turns = await runtime.store.get_turns(
                    session_id=runtime.session_id,
                    app_id=runtime.app_id,
                    limit=1,
                )
                self.assertEqual(
                    turns[-1]["metadata"]["structured_output"],
                    {"title": "Changelog", "commits_scanned": 5},
                )

    async def test_submit_request_closes_nested_structured_output_objects(self) -> None:
        nested_schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "details": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                    },
                    "required": ["count"],
                },
            },
            "required": ["summary", "details"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _StructuredOutputDefinition(Path(tmp))
                runtime = AgentRuntime.from_spec(definition, session_id="host-session", **_test_stores())
                await runtime.open()
                try:
                    await self._invoke_request(
                        runtime,
                        message="make a changelog",
                        structured_output=nested_schema,
                    )
                finally:
                    self.assertEqual(len(definition.provider.requests), 2)
                    finalizer_request = definition.provider.requests[-1]
                    self.assertFalse(
                        finalizer_request.provider_options["structured_output"][
                            "additionalProperties"
                        ]
                    )
                    self.assertFalse(
                        finalizer_request.provider_options["structured_output"][
                            "properties"
                        ]["details"]["additionalProperties"]
                    )
                    await runtime.shutdown()

                await runtime.shutdown()

    async def test_submit_request_binds_request_session_to_execution_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _SessionBindingDefinition(
                    Path(tmp),
                    expected_session_id="s-1",
                )
                runtime = AgentRuntime.from_spec(definition, session_id="host-session", **_test_stores())
                definition.runtime = runtime
                await runtime.open()

                await self._invoke_request(runtime, message="hi", session_id="s-1")

                self.assertTrue(definition.provider.asserted)
                await runtime.shutdown()

    async def test_submit_request_returns_compaction_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(agent_id="test-app", response_text="hello"),
                    session_id="host-session",
                    **_test_stores(),
                )
                await runtime.open()
                runtime.agent.config.compaction_token_threshold = 10

                with patch.object(
                    runtime, "get_session_total_tokens", side_effect=[11, 0]
                ):
                    with patch(
                        "mash.runtime.context.compact_conversation",
                        return_value=("summary text", "summary-turn"),
                    ):
                        _, _, result = await self._invoke_request(runtime, message="hi")

                self.assertEqual(result["compaction_summary_text"], "summary text")
                self.assertEqual(result["compaction_summary_trace_id"], "summary-turn")
                await runtime.shutdown()

    async def test_submit_request_continues_after_non_terminal_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    _ResponseThenFinishDefinition(Path(tmp)),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    accepted, events, result = await self._invoke_request(
                        runtime, message="hi"
                    )

                    think_events = [
                        event
                        for event in events
                        if event.get("event") == "agent.trace"
                        and (event.get("data") or {}).get("event_type")
                        == "runtime.llm.think.completed"
                    ]
                    stored_events = await runtime.runtime_store.list_request_events(
                        str(accepted["request_id"])
                    )
                    step_events = [
                        event
                        for event in stored_events
                        if event.event_type == RuntimeEventType.STEP_COMPLETED.value
                    ]
                    reasoning_trace = build_reasoning_trace(stored_events)

                    self.assertEqual(len(think_events), 2)
                    self.assertEqual(len(step_events), 2)
                    self.assertEqual(reasoning_trace["status"], "completed")
                    self.assertEqual(reasoning_trace["summary"]["total_steps"], 2)
                    self.assertEqual(
                        [item["step_index"] for item in reasoning_trace["steps"]],
                        [0, 1],
                    )
                    self.assertEqual(result["response"]["text"], "Final answer.")
                finally:
                    await runtime.shutdown()

    async def test_submit_request_applies_max_steps_for_response_only_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    _AlwaysRespondDefinition(Path(tmp)),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    _, _, result = await self._invoke_request(runtime, message="hi")

                    response_text = result["response"]["text"]
                    self.assertIn("max step limit", response_text)
                    self.assertEqual(
                        result["response"]["metadata"]["stop_reason"],
                        "max_steps",
                    )
                finally:
                    await runtime.shutdown()

    async def test_compact_session_assigns_trace_id_before_llm_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _CompactionLoggingDefinition(Path(tmp))
                runtime = AgentRuntime.from_spec(definition, session_id="host-session", **_test_stores())
                try:
                    await runtime.open()
                    await runtime.store.save_turn(
                        trace_id="turn-1",
                        session_id="s-compact",
                        app_id=runtime.app_id,
                        user_message="hello",
                        agent_response="world",
                        signals={},
                        session_total_tokens=4,
                        metadata={},
                    )

                    summary_text, trace_id = await runtime.compact_session("s-compact")

                    self.assertEqual(summary_text, "Summary:\n- compacted")
                    self.assertIsNotNone(trace_id)

                    llm = definition.last_built_llm
                    self.assertIsNotNone(llm)
                    assert llm is not None
                    self.assertIsNotNone(llm.trace_id_during_send)
                    self.assertEqual(llm.trace_ids_set[0], llm.trace_id_during_send)

                    logs = [
                        {
                            "event_type": event.event_type,
                            "trace_id": event.trace_id,
                            "payload": dict(event.payload or {}),
                        }
                        for event in await runtime.runtime_store.list_events(
                            runtime.app_id,
                            session_id="s-compact",
                        )
                    ]
                    llm_logs = [
                        event
                        for event in logs
                        if event["event_type"].startswith("llm.request.")
                    ]
                    self.assertEqual(
                        [event["event_type"] for event in llm_logs],
                        ["llm.request.start", "llm.request.complete"],
                    )
                    self.assertEqual(
                        {event["trace_id"] for event in llm_logs},
                        {llm.trace_id_during_send},
                    )
                    self.assertEqual(trace_id, llm.trace_id_during_send)
                finally:
                    await runtime.shutdown()

    async def test_submit_request_persists_unused_tool_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    _SignalsDefinition(Path(tmp)),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    _, _, result = await self._invoke_request(
                        runtime,
                        message="hi",
                        session_id="s-tools",
                    )

                    self.assertEqual(
                        result["response"]["signals"]["unused_tools"],
                        ["unused_tool"],
                    )
                    self.assertGreater(
                        int(result["response"]["signals"]["unused_tool_tokens"]),
                        0,
                    )

                    turns = await runtime.store.get_turns(
                        session_id="s-tools",
                        app_id=runtime.app_id,
                        limit=1,
                    )
                    self.assertEqual(len(turns), 1)
                    stored_turn = turns[0]
                    self.assertNotIn("tool_usage", stored_turn["metadata"])
                    self.assertGreater(
                        int(stored_turn["signals"]["unused_tool_tokens"]),
                        0,
                    )

                    latest_trace = await runtime.store.get_latest_trace(
                        app_id=runtime.app_id,
                        session_id="s-tools",
                    )
                    self.assertIsNotNone(latest_trace)
                    assert latest_trace is not None
                    self.assertNotIn("tool_usage", latest_trace["metadata"])
                finally:
                    await runtime.shutdown()

    async def test_submit_request_reuses_existing_mcp_server_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with patch(
                    "mash.mcp.host.Host.get_client",
                    return_value=_FakeMCPClient(),
                ) as get_client:
                    with patch(
                        "mash.mcp.manager.MCPManager._emit_event", return_value=None
                    ):
                        runtime = AgentRuntime.from_spec(
                            _MCPDefinition(Path(tmp)),
                            session_id="host-session",
                            **_test_stores(),
                        )
                        try:
                            await runtime.open()
                            original_add_server = runtime.mcp_manager.add_server

                            def fail_on_duplicate_registration(*args, **kwargs):
                                raise AssertionError(
                                    "configure_remote_tools should not re-add existing servers"
                                )

                            runtime.mcp_manager.add_server = fail_on_duplicate_registration  # type: ignore[method-assign]
                            _, _, first = await self._invoke_request(
                                runtime, message="first"
                            )
                            _, _, second = await self._invoke_request(
                                runtime, message="second"
                            )

                            self.assertEqual(first["response"]["text"], "ok")
                            self.assertEqual(second["response"]["text"], "ok")
                            self.assertEqual(
                                runtime.mcp_manager.list_servers(), ["github"]
                            )
                            self.assertEqual(get_client.call_count, 1)
                            self.assertEqual(runtime.definition.provider.call_count, 2)
                        finally:
                            if runtime.mcp_manager is not None:
                                runtime.mcp_manager.add_server = original_add_server  # type: ignore[method-assign]
                            await runtime.shutdown()

    async def test_web_search_tools_registered_with_plain_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with patch(
                    "mash.mcp.host.Host.get_client",
                    return_value=_FakeWebSearchClient(),
                ):
                    with patch(
                        "mash.mcp.manager.MCPManager._emit_event", return_value=None
                    ):
                        runtime = AgentRuntime.from_spec(
                            _WebSearchDefinition(Path(tmp)),
                            session_id="host-session",
                            **_test_stores(),
                        )
                        try:
                            await runtime.open()
                            _, _, result = await self._invoke_request(
                                runtime, message="hi"
                            )
                            self.assertEqual(result["response"]["text"], "ok")
                            tool_names = runtime.definition.provider.last_tool_names
                            self.assertIn("web_search", tool_names)
                            self.assertIn("web_fetch", tool_names)
                            self.assertNotIn(
                                "mcp_parallel_web_search_web_search", tool_names
                            )
                        finally:
                            await runtime.shutdown()

    async def test_web_search_tools_absent_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _WebSearchDefinition(Path(tmp))
                definition.build_web_search = lambda: None  # type: ignore[method-assign]
                runtime = AgentRuntime.from_spec(
                    definition,
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    _, _, result = await self._invoke_request(runtime, message="hi")
                    self.assertEqual(result["response"]["text"], "ok")
                    tool_names = runtime.definition.provider.last_tool_names
                    self.assertNotIn("web_search", tool_names)
                    self.assertNotIn("web_fetch", tool_names)
                finally:
                    await runtime.shutdown()

    async def test_completed_request_replays_buffered_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(agent_id="test-app", response_text="hello"),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    accepted = await runtime.submit_request(
                        message="hi", session_id="s1"
                    )
                    request_id = str(accepted["request_id"])
                    events = await self._collect_request_events(runtime, request_id)

                    event_names = [event["event"] for event in events]
                    self.assertEqual(event_names[0], "request.accepted")
                    self.assertIn("request.started", event_names)
                    self.assertEqual(event_names[-1], "request.completed")

                    replayed, _, done = await runtime.stream_response_events(
                        request_id,
                        cursor=0,
                        wait_timeout=0.0,
                    )
                    replay_names = [event["event"] for event in replayed]
                    self.assertEqual(replay_names, event_names)
                    self.assertTrue(done)
                finally:
                    await runtime.shutdown()

    async def test_request_error_emits_terminal_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(
                        agent_id="test-app",
                        response_text="ignored",
                        fail_on_message="boom",
                    ),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    accepted = await runtime.submit_request(
                        message="boom", session_id="s1"
                    )
                    request_id = str(accepted["request_id"])
                    events = await self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"].get("error", "")))
                finally:
                    await runtime.shutdown()

    async def test_request_error_marks_stream_done_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(
                        agent_id="test-app",
                        response_text="ignored",
                        fail_on_message="boom",
                    ),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    accepted = await runtime.submit_request(
                        message="boom", session_id="s1"
                    )
                    request_id = str(accepted["request_id"])
                    events = await self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    replayed, cursor, done = await runtime.stream_response_events(
                        request_id,
                        cursor=len(events),
                        wait_timeout=0.0,
                    )
                    self.assertEqual(replayed, [])
                    self.assertEqual(cursor, len(events))
                    self.assertTrue(done)
                finally:
                    await runtime.shutdown()

    async def test_same_session_requests_complete_without_waiting_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(
                        agent_id="test-app",
                        response_text="ok",
                        delay_seconds=0.25,
                    ),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    first = await runtime.submit_request(
                        message="first", session_id="s1"
                    )
                    second = await runtime.submit_request(
                        message="second", session_id="s1"
                    )
                    second_id = str(second["request_id"])
                    first_id = str(first["request_id"])

                    early_events, second_cursor, _ = (
                        await runtime.stream_response_events(
                            second_id,
                            cursor=0,
                            wait_timeout=0.1,
                        )
                    )
                    early_names = [event["event"] for event in early_events]
                    self.assertEqual(early_names[0], "request.accepted")

                    first_events = await self._collect_request_events(runtime, first_id)
                    second_events = await self._collect_request_events(
                        runtime, second_id
                    )

                    self.assertEqual(first_events[-1]["event"], "request.completed")
                    self.assertEqual(second_events[-1]["event"], "request.completed")
                    second_names = [event["event"] for event in second_events]
                    self.assertEqual(second_names[0], "request.accepted")
                    self.assertNotIn("request.waiting", second_names)
                    self.assertIn("request.started", second_names)
                finally:
                    await runtime.shutdown()

    async def test_different_sessions_can_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = AgentRuntime.from_spec(
                    build_spec(
                        agent_id="test-app",
                        response_text="ok",
                        delay_seconds=0.25,
                    ),
                    session_id="host-session",
                    **_test_stores(),
                )
                try:
                    await runtime.open()
                    first = await runtime.submit_request(
                        message="first", session_id="s1"
                    )
                    second = await runtime.submit_request(
                        message="second", session_id="s2"
                    )
                    first_events = await self._collect_request_events(
                        runtime, str(first["request_id"])
                    )
                    second_events = await self._collect_request_events(
                        runtime, str(second["request_id"])
                    )

                    self.assertIn(
                        "request.started", [event["event"] for event in second_events]
                    )
                    self.assertEqual(first_events[-1]["event"], "request.completed")
                    self.assertEqual(second_events[-1]["event"], "request.completed")
                finally:
                    await runtime.shutdown()


if __name__ == "__main__":
    unittest.main()

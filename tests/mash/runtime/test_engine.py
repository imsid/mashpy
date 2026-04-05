"""Tests for composed runtime engine behavior."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from mash.core.config import AgentConfig
from mash.core.context import Context, Response, ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.runtime.spec import AgentSpec
from mash.runtime.runtime import MashAgentRuntime
from mash.skills.registry import SkillRegistry
from mash.tools.base import FunctionTool, ToolResult
from mash.tools.registry import ToolRegistry


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

    def on_startup(self, runtime: MashAgentRuntime) -> None:
        del runtime
        self.startup_called = True

    def on_shutdown(self, runtime: MashAgentRuntime) -> None:
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


class _MismatchedDefinition(_BaseDefinition):
    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="different-app-id", system_prompt="Mismatch")


class MashAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def _collect_request_events(
        self,
        runtime: MashAgentRuntime,
        request_id: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        done = False
        while not done:
            chunk, cursor, done = await runtime.stream_request_events(
                request_id,
                cursor=cursor,
                wait_timeout=1.0,
            )
            events.extend(chunk)
        return events

    async def test_boots_and_shutdown_without_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _BaseDefinition(Path(tmp))
                runtime = MashAgentRuntime.from_spec(definition)
                self.assertTrue(definition.startup_called)
                self.assertFalse(hasattr(runtime, "renderer"))
                await runtime.shutdown()
                self.assertTrue(definition.shutdown_called)

    def test_app_id_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with self.assertRaises(ValueError):
                    MashAgentRuntime.from_spec(_MismatchedDefinition(Path(tmp)))

    async def test_process_user_message_saves_turn_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1", "token_usage": {"input": 3, "output": 2}},
                )
                with patch.object(runtime, "_run_agent", return_value=response):
                    result = await runtime.process_user_message("hi")

                self.assertEqual(result.session_total_tokens, 5)
                turns = await runtime.store.get_turns(
                    session_id=runtime.get_default_session_id(),
                    limit=1,
                )
                self.assertEqual(turns[-1]["user_message"], "hi")
                self.assertEqual(turns[-1]["agent_response"], "hello")
                self.assertEqual(turns[-1]["metadata"]["token_usage"]["input"], 3)

                await runtime.shutdown()

    async def test_process_user_message_binds_active_session_for_execution_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))

                def run_assertions(agent, _context: Context) -> Response:
                    self.assertEqual(agent.get_event_logger_session_id(), "s-1")
                    self.assertEqual(agent.llm.get_event_logger_session_id(), "s-1")
                    self.assertEqual(runtime.get_current_processing_session_id(), "s-1")
                    return Response(text="hello", context=Context(), metadata={})

                with patch.object(runtime, "_run_agent", side_effect=run_assertions):
                    await runtime.process_user_message("hi", session_id="s-1")

                self.assertEqual(runtime.agent._session_id, runtime.default_session_id)
                self.assertEqual(runtime.agent.llm.last_session_id, runtime.default_session_id)
                await runtime.shutdown()

    async def test_process_user_message_returns_compaction_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                runtime.agent.config.compaction_token_threshold = 10
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1", "token_usage": {"input": 1, "output": 1}},
                )

                with patch.object(runtime, "get_session_total_tokens", side_effect=[11, 0]):
                    with patch(
                        "mash.runtime.runtime.compact_conversation",
                        return_value=("summary text", "summary-turn"),
                    ):
                        with patch.object(runtime, "_run_agent", return_value=response):
                            result = await runtime.process_user_message("hi")

                self.assertEqual(result.compaction_summary_text, "summary text")
                self.assertEqual(result.compaction_summary_turn_id, "summary-turn")
                await runtime.shutdown()

    async def test_process_user_message_persists_unused_tool_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_SignalsDefinition(Path(tmp)))
                try:
                    result = await runtime.process_user_message("hi", session_id="s-tools")

                    self.assertEqual(
                        result.response.signals["unused_tools"],
                        ["unused_tool"],
                    )
                    self.assertGreater(
                        int(result.response.signals["unused_tool_tokens"]),
                        0,
                    )

                    turns = await runtime.store.get_turns(session_id="s-tools", limit=1)
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

    def test_set_subagent_ids_deduplicates_and_ignores_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                runtime.set_subagent_ids(["research", "", "analysis", "research", "  "])
                self.assertEqual(runtime.get_subagent_ids(), ["research", "analysis"])

    async def test_runtime_tools_use_active_session_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                token = runtime._current_session_id.set("s-42")
                try:
                    tool = runtime.agent.tools.get("set_user_preferences")
                    self.assertIsNotNone(tool)
                    result = await tool.execute({"preferences": {"tone": "brief"}})  # type: ignore[union-attr]
                    self.assertFalse(result.is_error)
                finally:
                    runtime._current_session_id.reset(token)

                self.assertEqual(
                    await runtime.store.get_preferences(app_id=runtime.app_id, session_id="s-42"),
                    {"tone": "brief"},
                )
                await runtime.shutdown()

    async def test_completed_request_replays_buffered_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1"},
                )
                try:
                    with patch.object(runtime, "_run_agent", return_value=response):
                        accepted = await runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = await self._collect_request_events(runtime, request_id)

                    event_names = [event["event"] for event in events]
                    self.assertEqual(event_names[0], "request.accepted")
                    self.assertIn("request.started", event_names)
                    self.assertEqual(event_names[-1], "request.completed")

                    replayed, _, done = await runtime.stream_request_events(
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
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    with patch.object(runtime, "process_user_message", side_effect=RuntimeError("boom")):
                        accepted = await runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = await self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"].get("error", "")))
                finally:
                    await runtime.shutdown()

    async def test_request_error_marks_stream_done_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    with patch.object(runtime, "process_user_message", side_effect=RuntimeError("boom")):
                        accepted = await runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = await self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    replayed, cursor, done = await runtime.stream_request_events(
                        request_id,
                        cursor=len(events),
                        wait_timeout=0.0,
                    )
                    self.assertEqual(replayed, [])
                    self.assertEqual(cursor, len(events))
                    self.assertTrue(done)
                finally:
                    await runtime.shutdown()

    async def test_same_session_overlap_emits_waiting_and_serializes_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    def slow_run(_agent, _context: Context) -> Response:
                        time.sleep(0.2)
                        return Response(
                            text="ok",
                            context=Context(),
                            metadata={},
                        )

                    with patch.object(runtime, "_run_agent", side_effect=slow_run):
                        first = await runtime.submit_request(message="first", session_id="s1")
                        first_events_prefix, first_cursor, _ = await runtime.stream_request_events(
                            str(first["request_id"]),
                            cursor=0,
                            wait_timeout=0.1,
                        )
                        prefix_names = [event["event"] for event in first_events_prefix]
                        if "request.started" not in prefix_names:
                            first_events_prefix, first_cursor, _ = await runtime.stream_request_events(
                                str(first["request_id"]),
                                cursor=first_cursor,
                                wait_timeout=0.1,
                            )
                            prefix_names.extend(event["event"] for event in first_events_prefix)
                        self.assertIn("request.started", prefix_names)
                        second = await runtime.submit_request(message="second", session_id="s1")
                        second_id = str(second["request_id"])
                        first_id = str(first["request_id"])

                        early_events, second_cursor, _ = await runtime.stream_request_events(
                            second_id,
                            cursor=0,
                            wait_timeout=0.1,
                        )
                        early_names = [event["event"] for event in early_events]
                        self.assertEqual(early_names, ["request.accepted"])
                        waiting_events, _, _ = await runtime.stream_request_events(
                            second_id,
                            cursor=second_cursor,
                            wait_timeout=0.1,
                        )
                        self.assertEqual(
                            [event["event"] for event in waiting_events],
                            ["request.waiting"],
                        )

                        first_events = await self._collect_request_events(runtime, first_id)
                        second_events = await self._collect_request_events(runtime, second_id)

                    self.assertEqual(first_events[-1]["event"], "request.completed")
                    self.assertEqual(second_events[-1]["event"], "request.completed")
                    second_names = [event["event"] for event in second_events]
                    self.assertEqual(second_names[:2], ["request.accepted", "request.waiting"])
                    self.assertIn("request.started", second_names)
                finally:
                    await runtime.shutdown()

    async def test_different_sessions_can_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(_BaseDefinition(Path(tmp)))
                started = asyncio.Event()
                release = asyncio.Event()
                concurrent_seen = 0
                current_running = 0
                lock = asyncio.Lock()

                async def slow_run(_agent, _context: Context) -> Response:
                    nonlocal concurrent_seen, current_running
                    async with lock:
                        current_running += 1
                        concurrent_seen = max(concurrent_seen, current_running)
                        if concurrent_seen >= 2:
                            started.set()
                    await asyncio.wait_for(release.wait(), timeout=1.0)
                    async with lock:
                        current_running -= 1
                    return Response(text="ok", context=Context(), metadata={})

                try:
                    with patch.object(runtime, "_run_agent", side_effect=slow_run):
                        first = await runtime.submit_request(message="first", session_id="s1")
                        second = await runtime.submit_request(message="second", session_id="s2")
                        await asyncio.wait_for(started.wait(), timeout=1.0)
                        release.set()
                        first_events = await self._collect_request_events(runtime, str(first["request_id"]))
                        second_events = await self._collect_request_events(runtime, str(second["request_id"]))

                    self.assertGreaterEqual(concurrent_seen, 2)
                    self.assertEqual(first_events[-1]["event"], "request.completed")
                    self.assertEqual(second_events[-1]["event"], "request.completed")
                finally:
                    await runtime.shutdown()

    async def test_agent_concurrency_cap_limits_parallel_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentRuntime.from_spec(
                    _BaseDefinition(Path(tmp)),
                )
                runtime.max_concurrent_requests = 1
                runtime._semaphore = asyncio.Semaphore(1)
                release = asyncio.Event()

                async def slow_run(_agent, _context: Context) -> Response:
                    await asyncio.wait_for(release.wait(), timeout=1.0)
                    return Response(text="ok", context=Context(), metadata={})

                try:
                    with patch.object(runtime, "_run_agent", side_effect=slow_run):
                        first = await runtime.submit_request(message="first", session_id="s1")
                        second = await runtime.submit_request(message="second", session_id="s2")
                        early_events, _, _ = await runtime.stream_request_events(
                            str(second["request_id"]),
                            cursor=0,
                            wait_timeout=0.05,
                        )
                        self.assertEqual(
                            [event["event"] for event in early_events],
                            ["request.accepted"],
                        )
                        release.set()
                        await self._collect_request_events(runtime, str(first["request_id"]))
                        second_events = await self._collect_request_events(runtime, str(second["request_id"]))

                    self.assertIn("request.started", [event["event"] for event in second_events])
                finally:
                    await runtime.shutdown()


if __name__ == "__main__":
    unittest.main()

"""Tests for composed runtime engine behavior."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mash.core.config import AgentConfig
from mash.core.context import Context, Response
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.runtime.spec import AgentSpec
from mash.runtime.server import MashAgentServer
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry


class _FakeLLMProvider(LLMProvider):
    def __init__(self) -> None:
        self.last_session_id: str | None = None
        self.last_app_id: str | None = None

    @property
    def model(self) -> str:
        return "test-model"

    def send(self, request: LLMRequest) -> LLMResponse:
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

    def on_startup(self, runtime: MashAgentServer) -> None:
        del runtime
        self.startup_called = True

    def on_shutdown(self, runtime: MashAgentServer) -> None:
        del runtime
        self.shutdown_called = True


class _MismatchedDefinition(_BaseDefinition):
    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="different-app-id", system_prompt="Mismatch")


class MashAgentServerTests(unittest.TestCase):
    def _collect_request_events(
        self,
        runtime: MashAgentServer,
        request_id: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        done = False
        while not done:
            chunk, cursor, done = runtime.stream_request_events(
                request_id,
                cursor=cursor,
                wait_timeout=1.0,
            )
            events.extend(chunk)
        return events

    def test_boots_and_shutdown_without_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                definition = _BaseDefinition(Path(tmp))
                runtime = MashAgentServer.from_spec(definition)
                self.assertTrue(definition.startup_called)
                self.assertFalse(hasattr(runtime, "renderer"))
                runtime.shutdown()
                self.assertTrue(definition.shutdown_called)

    def test_app_id_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with self.assertRaises(ValueError):
                    MashAgentServer.from_spec(_MismatchedDefinition(Path(tmp)))

    def test_process_user_message_saves_turn_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1", "token_usage": {"input": 3, "output": 2}},
                )
                with patch.object(runtime.agent, "run", return_value=response):
                    result = runtime.process_user_message("hi")

                self.assertEqual(result.session_total_tokens, 5)
                turns = runtime.store.get_turns(
                    session_id=runtime.get_default_session_id(),
                    limit=1,
                )
                self.assertEqual(turns[-1]["user_message"], "hi")
                self.assertEqual(turns[-1]["agent_response"], "hello")
                self.assertEqual(turns[-1]["metadata"]["token_usage"]["input"], 3)

    def test_process_user_message_rebinds_active_session_for_agent_and_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))

                def run_assertions(_context: Context) -> Response:
                    self.assertEqual(runtime.agent._session_id, "s-1")
                    self.assertEqual(runtime.agent.llm.last_session_id, "s-1")
                    return Response(text="hello", context=Context(), metadata={})

                with patch.object(runtime.agent, "run", side_effect=run_assertions):
                    runtime.process_user_message("hi", session_id="s-1")

                self.assertEqual(runtime.agent._session_id, runtime.default_session_id)
                self.assertEqual(runtime.agent.llm.last_session_id, runtime.default_session_id)

    def test_process_user_message_returns_compaction_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                runtime.agent.config.compaction_token_threshold = 10
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1", "token_usage": {"input": 1, "output": 1}},
                )

                with patch.object(runtime, "get_session_total_tokens", side_effect=[11, 0]):
                    with patch(
                        "mash.runtime.server.compact_conversation",
                        return_value=("summary text", "summary-turn"),
                    ):
                        with patch.object(runtime.agent, "run", return_value=response):
                            result = runtime.process_user_message("hi")

                self.assertEqual(result.compaction_summary_text, "summary text")
                self.assertEqual(result.compaction_summary_turn_id, "summary-turn")

    def test_set_subagent_ids_deduplicates_and_ignores_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                runtime.set_subagent_ids(["research", "", "analysis", "research", "  "])
                self.assertEqual(runtime.get_subagent_ids(), ["research", "analysis"])

    def test_runtime_tools_use_active_session_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                runtime._tool_context.session_id = "s-42"
                try:
                    tool = runtime.agent.tools.get("set_user_preferences")
                    self.assertIsNotNone(tool)
                    result = tool.execute({"preferences": {"tone": "brief"}})  # type: ignore[union-attr]
                    self.assertFalse(result.is_error)
                finally:
                    del runtime._tool_context.session_id

                self.assertEqual(
                    runtime.store.get_preferences(app_id=runtime.app_id, session_id="s-42"),
                    {"tone": "brief"},
                )

    def test_completed_request_replays_buffered_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                response = Response(
                    text="hello",
                    context=Context(),
                    metadata={"trace_id": "trace-1"},
                )
                try:
                    with patch.object(runtime.agent, "run", return_value=response):
                        accepted = runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = self._collect_request_events(runtime, request_id)

                    event_names = [event["event"] for event in events]
                    self.assertEqual(event_names[0], "request.accepted")
                    self.assertIn("request.started", event_names)
                    self.assertEqual(event_names[-1], "request.completed")

                    replayed, _, done = runtime.stream_request_events(
                        request_id,
                        cursor=0,
                        wait_timeout=0.0,
                    )
                    replay_names = [event["event"] for event in replayed]
                    self.assertEqual(replay_names, event_names)
                    self.assertTrue(done)
                finally:
                    runtime.shutdown()

    def test_request_error_emits_terminal_error_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    with patch.object(runtime, "process_user_message", side_effect=RuntimeError("boom")):
                        accepted = runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"].get("error", "")))
                finally:
                    runtime.shutdown()

    def test_request_error_marks_stream_done_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    with patch.object(runtime, "process_user_message", side_effect=RuntimeError("boom")):
                        accepted = runtime.submit_request(message="hi", session_id="s1")
                        request_id = str(accepted["request_id"])
                        events = self._collect_request_events(runtime, request_id)

                    self.assertEqual(events[-1]["event"], "request.error")
                    replayed, cursor, done = runtime.stream_request_events(
                        request_id,
                        cursor=len(events),
                        wait_timeout=0.0,
                    )
                    self.assertEqual(replayed, [])
                    self.assertEqual(cursor, len(events))
                    self.assertTrue(done)
                finally:
                    runtime.shutdown()

    def test_requests_are_single_flight_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                runtime = MashAgentServer.from_spec(_BaseDefinition(Path(tmp)))
                try:
                    def slow_run(_context: Context) -> Response:
                        time.sleep(0.2)
                        return Response(
                            text="ok",
                            context=Context(),
                            metadata={},
                        )

                    with patch.object(runtime.agent, "run", side_effect=slow_run):
                        first = runtime.submit_request(message="first", session_id="s1")
                        second = runtime.submit_request(message="second", session_id="s1")
                        second_id = str(second["request_id"])
                        first_id = str(first["request_id"])

                        early_events, _, _ = runtime.stream_request_events(
                            second_id,
                            cursor=0,
                            wait_timeout=0.05,
                        )
                        early_names = [event["event"] for event in early_events]
                        self.assertEqual(early_names, ["request.accepted"])

                        first_events = self._collect_request_events(runtime, first_id)
                        second_events = self._collect_request_events(runtime, second_id)

                    self.assertEqual(first_events[-1]["event"], "request.completed")
                    self.assertEqual(second_events[-1]["event"], "request.completed")
                    self.assertIn("request.started", [event["event"] for event in second_events])
                finally:
                    runtime.shutdown()


if __name__ == "__main__":
    unittest.main()

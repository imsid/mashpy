"""Tests for the built-in Masher subagent."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from mash.agents import MasherAgentSpec
from mash.agents.masher.tool import AppendJsonlTool, GetTraceLogsTool
from mash.core.config import AgentConfig
from mash.core.context import Context, Response
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.memory.store import SQLiteStore
from mash.runtime import AgentSpec, MashAgentHostBuilder
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry


class _FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    def send(self, request: LLMRequest) -> LLMResponse:
        del request
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _PrimarySpec(AgentSpec):
    def __init__(self, app_id: str = "primary") -> None:
        self.app_id = app_id

    def get_agent_id(self) -> str:
        return self.app_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt=(
                "You are the primary agent. Use InvokeSubagent(agent_id, prompt, opts) "
                "when a specialized subagent can help."
            ),
        )


class MasherTests(unittest.TestCase):
    def _build_target_files(self, tmp: str) -> tuple[Path, SQLiteStore]:
        data_dir = Path(tmp) / "primary"
        logs_dir = data_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "events.jsonl"
        store = SQLiteStore(data_dir / "state.db")
        return log_path, store

    def _save_turn(
        self,
        store: SQLiteStore,
        *,
        trace_id: str,
        session_id: str,
        app_id: str = "primary",
        user_message: str = "user",
        agent_response: str = "assistant",
    ) -> None:
        store.save_turn(
            trace_id=trace_id,
            session_id=session_id,
            app_id=app_id,
            user_message=user_message,
            agent_response=agent_response,
            signals={},
            session_total_tokens=100,
            metadata={"trace_id": trace_id},
        )

    def test_builder_enable_masher_false_leaves_builder_unchanged(self) -> None:
        host = MashAgentHostBuilder().primary(_PrimarySpec()).enable_masher(False).build()
        try:
            described = {item["agent_id"]: item for item in host.describe_agents()}
            self.assertEqual(sorted(described.keys()), ["primary"])
        finally:
            host.close()

    def test_spec_registers_store_tools_bash_jsonl_tool_and_eval_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(log_file="primary/logs/events.jsonl")

                tools = spec.build_tools()
                skills = spec.build_skills()

                self.assertIn("get_latest_session", tools)
                self.assertIn("get_latest_trace", tools)
                self.assertIn("list_recent_traces", tools)
                self.assertIn("get_trace_logs", tools)
                self.assertIn("bash", tools)
                self.assertIn("append_jsonl", tools)
                self.assertEqual(
                    sorted(skill.name for skill in skills.list_skills()),
                    ["online-eval-curation"],
                )
                prompt = spec.build_agent_config().system_prompt
                self.assertIn("event_type", prompt)
                self.assertIn("get_latest_session", prompt)
                self.assertIn("get_trace_logs", prompt)
                self.assertEqual(spec.build_agent_config().max_steps, 6)

    def test_relative_data_dir_resolves_once_for_primary_and_masher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"MASH_DATA_DIR": ".mash"}, clear=False):
                    primary = _PrimarySpec()
                    expected = (Path(tmp) / ".mash" / "primary" / "logs" / "events.jsonl").resolve()
                    self.assertEqual(primary.get_log_destination(), expected)

                    spec = MasherAgentSpec(log_file=primary.get_log_destination())
                    self.assertEqual(spec.log_file, expected)
            finally:
                os.chdir(previous_cwd)

    def test_store_tools_resolve_latest_session_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path, store = self._build_target_files(tmp)
            self._save_turn(store, trace_id="t-1", session_id="s-1")
            self._save_turn(store, trace_id="t-2a", session_id="s-2")
            self._save_turn(store, trace_id="t-2b", session_id="s-2")

            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(log_file=log_path, target_app_id="primary")
                tools = spec.build_tools()

                latest_session = tools.get("get_latest_session").execute({})
                latest_trace = tools.get("get_latest_trace").execute(
                    {"session_id": "s-2"}
                )

                self.assertFalse(latest_session.is_error)
                self.assertFalse(latest_trace.is_error)
                self.assertEqual(json.loads(latest_session.content)["session_id"], "s-2")
                self.assertEqual(json.loads(latest_trace.content)["trace_id"], "t-2b")

    def test_list_recent_traces_defaults_to_latest_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path, store = self._build_target_files(tmp)
            self._save_turn(store, trace_id="t-1", session_id="s-1")
            self._save_turn(store, trace_id="t-2a", session_id="s-2")
            self._save_turn(store, trace_id="t-2b", session_id="s-2")
            self._save_turn(store, trace_id="t-2c", session_id="s-2")

            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(log_file=log_path, target_app_id="primary")
                result = spec.build_tools().get("list_recent_traces").execute(
                    {"limit": 2}
                )

                self.assertFalse(result.is_error)
                payload = json.loads(result.content)
                self.assertEqual(payload["session_id"], "s-2")
                self.assertEqual(
                    [item["trace_id"] for item in payload["traces"]],
                    ["t-2c", "t-2b"],
                )

    def test_get_trace_logs_returns_requested_trace_within_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path, _store = self._build_target_files(tmp)
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event_type": "agent.run.start",
                                "session_id": "s-1",
                                "trace_id": "t-1a",
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "agent.tool.call",
                                "session_id": "s-1",
                                "trace_id": "t-1a",
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "agent.run.start",
                                "session_id": "s-1",
                                "trace_id": "t-1b",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            tool = GetTraceLogsTool(log_path)
            result = tool.execute({"session_id": "s-1", "trace_id": "t-1a"})

            self.assertFalse(result.is_error)
            payload = json.loads(result.content)
            self.assertEqual(payload["session_id"], "s-1")
            self.assertEqual(payload["trace_id"], "t-1a")
            self.assertEqual(len(payload["events"]), 2)
            self.assertEqual(payload["events"][1]["event_type"], "agent.tool.call")

    def test_append_jsonl_appends_and_skips_duplicate_session_trace_pair(self) -> None:
        tool = AppendJsonlTool()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "primary" / "evals" / "online_evals.jsonl"
            record = {
                "source_log_path": ".mash/primary/logs/events.jsonl",
                "trace_id": "trace-1",
                "session_id": "s-1",
                "tools_called": ["search_conversations"],
            }

            first = tool.execute({"path": str(path), "record": record})
            second = tool.execute({"path": str(path), "record": record})

            self.assertFalse(first.is_error)
            self.assertFalse(second.is_error)
            self.assertTrue(first.metadata["appended"])
            self.assertFalse(second.metadata["appended"])

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["trace_id"], "trace-1")

    def test_builder_enable_masher_registers_subagent_and_primary_prompt_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                with patch.object(
                    MasherAgentSpec,
                    "build_llm",
                    return_value=_FakeLLMProvider(),
                ):
                    host = MashAgentHostBuilder().primary(_PrimarySpec()).enable_masher().build()
                    host.start()
                    try:
                        described = {item["agent_id"]: item for item in host.describe_agents()}
                        self.assertIn("masher", described)
                        self.assertEqual(described["masher"]["role"], "subagent")

                        primary = host.get_agent("primary")
                        masher = host.get_agent("masher")
                        self.assertEqual(primary.get_subagent_ids(), ["masher"])
                        self.assertIn("Masher", str(primary.system_prompt))
                        self.assertIn("get_latest_session", masher.agent.tools)
                        self.assertIn("get_latest_trace", masher.agent.tools)
                        self.assertIn("list_recent_traces", masher.agent.tools)
                        self.assertIn("get_trace_logs", masher.agent.tools)
                        self.assertIn("append_jsonl", masher.agent.tools)
                        self.assertIn("search_conversations", masher.agent.tools)
                        self.assertIn("Skill", masher.agent.tools)
                        self.assertIn(
                            str(primary.definition.get_log_destination()),
                            str(masher.agent.config.system_prompt),
                        )
                    finally:
                        host.close()

    def test_invoke_subagent_stores_delegated_prompt_in_masher_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                with patch.object(
                    MasherAgentSpec,
                    "build_llm",
                    return_value=_FakeLLMProvider(),
                ):
                    host = MashAgentHostBuilder().primary(_PrimarySpec()).enable_masher().build()
                    host.start()
                    try:
                        primary = host.get_agent("primary")
                        masher = host.get_agent("masher")
                        tool = primary.agent.tools.get("InvokeSubagent")
                        self.assertIsNotNone(tool)

                        response = Response(
                            text="Masher review complete",
                            context=Context(),
                            metadata={"trace_id": "trace-masher"},
                        )
                        delegated_prompt = "What happened in the most recent session?"
                        with patch.object(masher.agent, "run", return_value=response):
                            tool.execute(  # type: ignore[union-attr]
                                {
                                    "agent_id": "masher",
                                    "prompt": delegated_prompt,
                                    "opts": {"timeout_ms": 1500},
                                }
                            )

                        turns = masher.store.list_sessions(app_id="masher")
                        self.assertEqual(len(turns), 1)
                        session_id = turns[0]["session_id"]
                        stored_turns = masher.store.get_turns(session_id=session_id, limit=1)
                        self.assertEqual(delegated_prompt, stored_turns[-1]["user_message"])
                    finally:
                        host.close()


if __name__ == "__main__":
    unittest.main()

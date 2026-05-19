"""Tests for the built-in Masher subagent."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from mash.agents import MasherAgentSpec
from mash.agents.masher import (
    MASHER_ONLINE_EVAL_WORKFLOW_ID,
    MASHER_TRACE_DIGEST_WORKFLOW_ID,
)
from mash.agents.masher.tool import (
    AppendJsonlTool,
    GetTraceEventsTool,
    _build_online_eval_row,
    _load_trace_bundle,
)
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.memory.store import SQLiteStore
from mash.runtime import AgentSpec, HostBuilder
from mash.runtime.events.types import RuntimeEvent
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import build_spec
from mash.tools.registry import ToolRegistry


class _FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
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


class _FakeRuntimeStore:
    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []

    def append(
        self,
        *,
        app_id: str,
        session_id: str,
        trace_id: str,
        event_type: str,
        created_at: float,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._events.append(
            RuntimeEvent(
                event_id=len(self._events) + 1,
                app_id=app_id,
                agent_id=app_id,
                session_id=session_id,
                trace_id=trace_id,
                event_type=event_type,
                payload=dict(payload or {}),
                created_at=created_at,
            )
        )

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        events = [
            event
            for event in self._events
            if event.app_id == app_id
            and event.event_id > int(after_event_id)
            and (session_id is None or event.session_id == session_id)
            and (trace_id is None or event.trace_id == trace_id)
        ]
        if limit is not None:
            return events[: max(1, int(limit))]
        return events

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        traces = await self.list_recent_traces(app_id, session_id=session_id, limit=1)
        return traces[0] if traces else None

    async def list_recent_traces(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str | None], list[RuntimeEvent]] = {}
        for event in self._events:
            if event.app_id != app_id or event.trace_id is None:
                continue
            if session_id is not None and event.session_id != session_id:
                continue
            grouped.setdefault((event.trace_id, event.session_id), []).append(event)
        summaries: list[dict[str, Any]] = []
        for (trace_id_value, session_id_value), trace_events in grouped.items():
            trace_events.sort(key=lambda item: item.event_id)
            summaries.append(
                {
                    "trace_id": trace_id_value,
                    "session_id": session_id_value,
                    "event_count": len(trace_events),
                    "started_at": float(trace_events[0].created_at),
                    "latest_event_at": float(trace_events[-1].created_at),
                    "latest_event_id": int(trace_events[-1].event_id),
                }
            )
        summaries.sort(
            key=lambda item: (item["latest_event_at"], item["latest_event_id"]),
            reverse=True,
        )
        return summaries[: max(1, int(limit))]


class MasherTests(unittest.TestCase):
    def _primary_spec(self) -> AgentSpec:
        return build_spec(agent_id="primary", response_text="primary-ok")

    def _build_target_files(self, tmp: str) -> tuple[Path, SQLiteStore, _FakeRuntimeStore]:
        data_dir = Path(tmp) / "primary"
        data_dir.mkdir(parents=True, exist_ok=True)
        store_path = data_dir / "state.db"
        store = SQLiteStore(store_path)
        return store_path, store, _FakeRuntimeStore()

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
        asyncio.run(
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
        )

    def _save_trace_log(
        self,
        store: _FakeRuntimeStore,
        *,
        session_id: str,
        trace_id: str,
        event_type: str = "agent.run.start",
        app_id: str = "primary",
        created_at: float = 1.0,
        payload: dict[str, object] | None = None,
        loop_index: int | None = None,
    ) -> None:
        store._events.append(
            RuntimeEvent(
                event_id=len(store._events) + 1,
                app_id=app_id,
                agent_id=app_id,
                session_id=session_id,
                trace_id=trace_id,
                event_type=event_type,
                payload=dict(payload or {}),
                created_at=created_at,
                loop_index=loop_index,
            )
        )

    def test_builder_enable_masher_false_leaves_builder_unchanged(self) -> None:
        host = HostBuilder().primary(self._primary_spec()).enable_masher(False).build()
        try:
            described = {item["agent_id"]: item for item in host.describe_agents()}
            self.assertEqual(sorted(described.keys()), ["primary"])
        finally:
            asyncio.run(host.close())

    def test_spec_registers_store_tools_bash_jsonl_tool_and_eval_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(
                    log_store=SQLiteStore(Path(tmp) / "primary" / "state.db"),
                    target_app_id="primary",
                )

                tools = spec.build_tools()
                skills = spec.build_skills()

                self.assertIn("get_latest_session", tools)
                self.assertIn("get_latest_trace", tools)
                self.assertIn("list_recent_traces", tools)
                self.assertIn("get_trace_events", tools)
                self.assertIn("bash", tools)
                self.assertIn("append_jsonl", tools)
                self.assertIn("list_traces_since", tools)
                self.assertIn("run_trace_digest_workflow", tools)
                self.assertIn("run_online_eval_curation_workflow", tools)
                self.assertEqual(
                    sorted(skill.name for skill in skills.list_skills()),
                    ["online-eval-curation", "trace-digest-workflow"],
                )
                prompt = spec.build_agent_config().system_prompt
                self.assertIn("event_type", prompt)
                self.assertIn("workflow_input", prompt)
                self.assertIn("run_trace_digest_workflow", prompt)
                self.assertIn("run_online_eval_curation_workflow", prompt)
                self.assertIn("masher-online-eval-curation", prompt)
                self.assertEqual(spec.build_agent_config().max_steps, 6)

    def test_relative_data_dir_resolves_once_for_primary_and_masher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {"MASH_DATA_DIR": ".mash", "MASH_MEMORY_DATABASE_URL": ""},
                    clear=False,
                ):
                    primary = _PrimarySpec()
                    expected = (Path(tmp) / ".mash" / "primary" / "state.db").resolve()
                    self.assertIsInstance(primary.get_log_destination(), SQLiteStore)

                    spec = MasherAgentSpec(
                        log_store=primary.get_log_destination(),
                        target_app_id="primary",
                    )
                    self.assertEqual(spec.store_path, expected)
            finally:
                os.chdir(previous_cwd)

    def test_store_tools_resolve_latest_session_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_turn(store, trace_id="t-1", session_id="s-1")
            self._save_turn(store, trace_id="t-2a", session_id="s-2")
            self._save_turn(store, trace_id="t-2b", session_id="s-2")
            self._save_trace_log(runtime_store, session_id="s-1", trace_id="t-1", created_at=1.0)
            self._save_trace_log(runtime_store, session_id="s-2", trace_id="t-2a", created_at=2.0)
            self._save_trace_log(runtime_store, session_id="s-2", trace_id="t-2b", created_at=3.0)

            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(
                    log_store=store,
                    target_app_id="primary",
                    runtime_store=runtime_store,
                )
                tools = spec.build_tools()

                latest_session = asyncio.run(tools.get("get_latest_session").execute({}))
                latest_trace = asyncio.run(
                    tools.get("get_latest_trace").execute({"session_id": "s-2"})
                )

                self.assertFalse(latest_session.is_error)
                self.assertFalse(latest_trace.is_error)
                self.assertEqual(json.loads(latest_session.content)["session_id"], "s-2")
                self.assertEqual(json.loads(latest_trace.content)["trace_id"], "t-2b")

    def test_list_recent_traces_defaults_to_latest_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_turn(store, trace_id="t-1", session_id="s-1")
            self._save_turn(store, trace_id="t-2a", session_id="s-2")
            self._save_turn(store, trace_id="t-2b", session_id="s-2")
            self._save_turn(store, trace_id="t-2c", session_id="s-2")
            self._save_trace_log(runtime_store, session_id="s-1", trace_id="t-1", created_at=1.0)
            self._save_trace_log(runtime_store, session_id="s-2", trace_id="t-2a", created_at=2.0)
            self._save_trace_log(runtime_store, session_id="s-2", trace_id="t-2b", created_at=3.0)
            self._save_trace_log(runtime_store, session_id="s-2", trace_id="t-2c", created_at=4.0)

            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec(
                    log_store=store,
                    target_app_id="primary",
                    runtime_store=runtime_store,
                )
                result = asyncio.run(
                    spec.build_tools().get("list_recent_traces").execute({"limit": 2})
                )

                self.assertFalse(result.is_error)
                payload = json.loads(result.content)
                self.assertEqual(payload["session_id"], "s-2")
                self.assertEqual(
                    [item["trace_id"] for item in payload["traces"]],
                    ["t-2c", "t-2b"],
                )

    def test_get_trace_events_returns_requested_trace_within_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, _store, runtime_store = self._build_target_files(tmp)
            self._save_trace_log(runtime_store, session_id="s-1", trace_id="t-1a", created_at=1.0)
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1a",
                event_type="agent.tool.call",
                created_at=2.0,
                payload={"tool_name": "search_conversations", "tool_call_id": "call-1"},
            )
            self._save_trace_log(runtime_store, session_id="s-1", trace_id="t-1b", created_at=3.0)

            tool = GetTraceEventsTool(
                runtime_store=runtime_store,
                runtime_database_url=None,
                app_id="primary",
            )
            result = asyncio.run(tool.execute({"session_id": "s-1", "trace_id": "t-1a"}))

            self.assertFalse(result.is_error)
            payload = json.loads(result.content)
            self.assertEqual(payload["session_id"], "s-1")
            self.assertEqual(payload["trace_id"], "t-1a")
            self.assertEqual(len(payload["events"]), 2)
            self.assertEqual(payload["events"][1]["event_type"], "agent.tool.call")

    def test_trace_digest_workflow_trace_mode_returns_digest_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_turn(store, trace_id="t-1", session_id="s-1")
            self._save_trace_log(runtime_store, session_id="s-1", trace_id="t-1", created_at=1.0)
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="llm.request.complete",
                created_at=2.0,
                payload={"input_tokens": 10, "output_tokens": 4},
            )
            artifact_path = Path(tmp) / "masher" / "trace-digests.jsonl"

            spec = MasherAgentSpec(
                log_store=store,
                target_app_id="primary",
                runtime_store=runtime_store,
                trace_digest_jsonl_path=artifact_path,
            )
            result = asyncio.run(
                spec.build_tools().get("run_trace_digest_workflow").execute(
                    {
                        "workflow_input": {
                            "mode": "trace",
                            "target_agent_id": "primary",
                            "session_id": "s-1",
                            "trace_id": "t-1",
                        },
                        "task_state": {},
                    }
                )
            )

            self.assertFalse(result.is_error)
            digest = json.loads(result.content)
            self.assertEqual(digest["target_agent_id"], "primary")
            self.assertEqual(digest["session_id"], "s-1")
            self.assertEqual(digest["trace_id"], "t-1")
            self.assertEqual(digest["metrics"]["input_tokens"], 10)
            self.assertFalse(artifact_path.exists())

    def test_shared_trace_bundle_extracts_eval_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, _store, runtime_store = self._build_target_files(tmp)
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="agent.run.start",
                created_at=1.0,
                payload={"user_message": "How should this work?"},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.tool.call.completed",
                created_at=2.0,
                payload={"tool_name": "bash"},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.step.completed",
                created_at=3.0,
                payload={},
                loop_index=0,
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="llm.request.complete",
                created_at=4.0,
                payload={"input_tokens": 12, "output_tokens": 5},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.completed",
                created_at=5.0,
                payload={"response": {"text": "It works like this."}},
            )

            bundle = asyncio.run(
                _load_trace_bundle(
                    runtime_store,
                    target_agent_id="primary",
                    session_id="s-1",
                    trace_id="t-1",
                )
            )
            row = _build_online_eval_row(bundle)

            self.assertEqual(row["user_message"], "How should this work?")
            self.assertEqual(row["assistant_response"], "It works like this.")
            self.assertEqual(row["tools_called"], ["bash"])
            self.assertEqual(row["tool_call_count"], 1)
            self.assertEqual(row["step_count"], 1)
            self.assertEqual(row["input_tokens"], 12)
            self.assertEqual(row["output_tokens"], 5)

    def test_trace_digest_workflow_incremental_mode_writes_jsonl_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_trace_log(runtime_store, session_id="s-old", trace_id="t-old", created_at=1.0)
            self._save_trace_log(runtime_store, session_id="s-new", trace_id="t-new", created_at=3.0)
            self._save_trace_log(
                runtime_store,
                session_id="s-new",
                trace_id="t-new",
                event_type="agent.tool.error",
                created_at=4.0,
                payload={"error": "boom"},
            )
            artifact_path = Path(tmp) / "masher" / "trace-digests.jsonl"

            spec = MasherAgentSpec(
                log_store=store,
                target_app_id="primary",
                runtime_store=runtime_store,
                trace_digest_jsonl_path=artifact_path,
            )
            result = asyncio.run(
                spec.build_tools().get("run_trace_digest_workflow").execute(
                    {
                        "workflow_input": {
                            "mode": "incremental",
                            "target_agent_id": "primary",
                        },
                        "task_state": {
                            "schema_version": 1,
                            "checkpoints": {
                                "primary": {
                                    "last_run_ts": 2.0,
                                    "last_trace_ids": ["t-old"],
                                }
                            },
                        },
                    }
                )
            )

            self.assertFalse(result.is_error)
            next_state = json.loads(result.content)
            self.assertEqual(next_state["processed_trace_count"], 1)
            self.assertEqual(next_state["appended_trace_count"], 1)
            self.assertEqual(
                next_state["checkpoints"]["primary"]["last_trace_ids"],
                ["t-new"],
            )
            lines = artifact_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            digest = json.loads(lines[0])
            self.assertEqual(digest["trace_id"], "t-new")
            self.assertEqual(digest["status"], "failed")

    def test_online_eval_workflow_trace_mode_writes_dataset_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="agent.run.start",
                created_at=1.0,
                payload={"user_message": "Question"},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.completed",
                created_at=2.0,
                payload={"response": {"text": "Answer"}},
            )
            artifact_path = Path(tmp) / "masher" / "online-evals.jsonl"

            spec = MasherAgentSpec(
                log_store=store,
                target_app_id="primary",
                runtime_store=runtime_store,
                online_eval_jsonl_path=artifact_path,
            )
            result = asyncio.run(
                spec.build_tools().get("run_online_eval_curation_workflow").execute(
                    {
                        "workflow_input": {
                            "mode": "trace",
                            "target_agent_id": "primary",
                            "session_id": "s-1",
                            "trace_id": "t-1",
                        },
                        "task_state": {},
                    }
                )
            )

            self.assertFalse(result.is_error)
            payload = json.loads(result.content)
            self.assertTrue(payload["appended"])
            self.assertNotIn("summary", payload["record"])
            self.assertEqual(payload["record"]["user_message"], "Question")
            self.assertEqual(payload["record"]["assistant_response"], "Answer")
            lines = artifact_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["trace_id"], "t-1")

    def test_online_eval_workflow_incremental_skips_duplicate_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            self._save_trace_log(
                runtime_store,
                session_id="s-new",
                trace_id="t-new",
                event_type="agent.run.start",
                created_at=3.0,
                payload={"user_message": "Question"},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-new",
                trace_id="t-new",
                event_type="runtime.request.completed",
                created_at=4.0,
                payload={"response": {"text": "Answer"}},
            )
            artifact_path = Path(tmp) / "masher" / "online-evals.jsonl"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json.dumps(
                    {
                        "target_agent_id": "primary",
                        "session_id": "s-new",
                        "trace_id": "t-new",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            spec = MasherAgentSpec(
                log_store=store,
                target_app_id="primary",
                runtime_store=runtime_store,
                online_eval_jsonl_path=artifact_path,
            )
            result = asyncio.run(
                spec.build_tools().get("run_online_eval_curation_workflow").execute(
                    {
                        "workflow_input": {
                            "mode": "incremental",
                            "target_agent_id": "primary",
                        },
                        "task_state": {"checkpoints": {"primary": {"last_run_ts": 2.0}}},
                    }
                )
            )

            self.assertFalse(result.is_error)
            next_state = json.loads(result.content)
            self.assertEqual(next_state["processed_trace_count"], 1)
            self.assertEqual(next_state["appended_trace_count"], 0)
            self.assertEqual(
                next_state["checkpoints"]["primary"]["last_trace_ids"],
                ["t-new"],
            )
            self.assertEqual(len(artifact_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_trace_digest_workflow_rejects_missing_trace_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _store_path, store, runtime_store = self._build_target_files(tmp)
            spec = MasherAgentSpec(
                log_store=store,
                target_app_id="primary",
                runtime_store=runtime_store,
            )
            result = asyncio.run(
                spec.build_tools().get("run_trace_digest_workflow").execute(
                    {
                        "workflow_input": {
                            "mode": "trace",
                            "target_agent_id": "primary",
                            "session_id": "s-1",
                        },
                        "task_state": {},
                    }
                )
            )

            self.assertTrue(result.is_error)
            self.assertIn("workflow_input.trace_id is required", result.content)

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

            first = asyncio.run(tool.execute({"path": str(path), "record": record}))
            second = asyncio.run(tool.execute({"path": str(path), "record": record}))

            self.assertFalse(first.is_error)
            self.assertFalse(second.is_error)
            self.assertTrue(first.metadata["appended"])
            self.assertFalse(second.metadata["appended"])

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["trace_id"], "trace-1")

    def test_builder_enable_masher_registers_hidden_workflow_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                host = HostBuilder().primary(self._primary_spec()).enable_masher().build()
                try:
                    described = {item["agent_id"]: item for item in host.describe_agents()}
                    self.assertNotIn("masher", described)
                    self.assertNotIn("masher", host.list_agents())
                    workflows = {
                        workflow.workflow_id
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertIn(MASHER_TRACE_DIGEST_WORKFLOW_ID, workflows)
                    self.assertIn(MASHER_ONLINE_EVAL_WORKFLOW_ID, workflows)
                finally:
                    asyncio.run(host.close())


if __name__ == "__main__":
    unittest.main()

"""Tests for the built-in Masher subagent."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from mash.agents import MasherAgentSpec
from mash.agents.masher import (
    MASHER_ONLINE_EVAL_STRUCTURED_OUTPUT,
    MASHER_ONLINE_EVAL_WORKFLOW_ID,
    MASHER_TRACE_DIGEST_STRUCTURED_OUTPUT,
    MASHER_TRACE_DIGEST_WORKFLOW_ID,
)
from mash.agents.masher.tool import (
    _build_online_eval_row,
    _load_trace_bundle,
    _load_trace_events,
)
from mash.runtime.events import build_runtime_trace, build_span_tree, analyze_trace
from mash.core.agent import Agent
from mash.core.llm import LLMProvider, OSSCompatibleProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.runtime import AgentSpec, HostBuilder
from mash.testing.runtime_fixtures import metadata
from mash.runtime.events.types import RuntimeEvent
from mash.testing.runtime_fixtures import build_spec


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

    def _build_runtime_store(self) -> _FakeRuntimeStore:
        return _FakeRuntimeStore()

    def _build_masher_spec(
        self,
        *,
        runtime_store: _FakeRuntimeStore | None = None,
        trace_digest_jsonl_path: Path | None = None,
        online_eval_jsonl_path: Path | None = None,
    ) -> MasherAgentSpec:
        spec = MasherAgentSpec()
        if runtime_store is not None:
            spec.runtime_context.bind_runtime_store(runtime_store)
        if trace_digest_jsonl_path is not None:
            spec.runtime_context.trace_digest_jsonl_path = trace_digest_jsonl_path
        if online_eval_jsonl_path is not None:
            spec.runtime_context.online_eval_jsonl_path = online_eval_jsonl_path
        return spec

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
        host = HostBuilder().agent(self._primary_spec(), metadata=metadata()).enable_masher(False).build()
        try:
            described = {item["agent_id"]: item for item in host.describe_agents()}
            self.assertEqual(sorted(described.keys()), ["primary"])
        finally:
            asyncio.run(host.close())

    def test_build_llm_falls_back_to_oss_when_base_url_and_model_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "MASH_DATA_DIR": tmp,
                    "OSS_BASE_URL": "http://gpu-box:8000/v1",
                    "MASHER_OSS_MODEL": "Qwen/Qwen3-32B",
                },
                clear=True,
            ):
                provider = MasherAgentSpec().build_llm()
                self.assertIsInstance(provider, OSSCompatibleProvider)

    def test_build_llm_raises_when_oss_base_url_set_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "OSS_BASE_URL": "http://gpu-box:8000/v1"},
                clear=True,
            ):
                with self.assertRaises(RuntimeError):
                    MasherAgentSpec().build_llm()

    def test_build_llm_raises_without_any_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                with self.assertRaises(RuntimeError):
                    MasherAgentSpec().build_llm()

    def test_spec_registers_only_workflow_tools_and_normal_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec()

                tools = spec.build_tools()
                skills = spec.build_skills()

                self.assertEqual(
                    sorted(tools.list_tools()),
                    [
                        "run_online_eval_curation_workflow",
                        "run_trace_digest_workflow",
                    ],
                )
                agent = Agent(
                    llm=_FakeLLMProvider(),
                    tools=tools,
                    skills=skills,
                    config=spec.build_agent_config(),
                )
                self.assertEqual(
                    sorted(agent.tools.list_tools()),
                    [
                        "Skill",
                        "run_online_eval_curation_workflow",
                        "run_trace_digest_workflow",
                    ],
                )
                self.assertEqual(
                    sorted(skill.name for skill in skills.list_skills()),
                    ["gen-synthetic-evals", "online-eval-curation", "score-evals", "trace-digest-workflow"],
                )
                prompt = spec.build_agent_config().system_prompt
                self.assertIn("event_type", prompt)
                self.assertIn("workflow_input", prompt)
                self.assertIn("masher-trace-digest", prompt)
                self.assertIn("digest-traces", prompt)
                self.assertIn("trace-digest-workflow", prompt)
                self.assertIn("masher-online-eval-curation", prompt)
                self.assertIn("curate-online-evals", prompt)
                self.assertIn("online-eval-curation", prompt)
                self.assertIn("Call the standard Skill tool exactly once", prompt)
                self.assertNotIn("Trace Digest Workflow", prompt)
                self.assertNotIn("Online Eval Curation", prompt)
                self.assertNotIn("run_trace_digest_workflow", prompt)
                self.assertNotIn("run_online_eval_curation_workflow", prompt)
                self.assertEqual(spec.build_agent_config().max_steps, 6)

    def test_relative_data_dir_resolves_once_for_masher_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {"MASH_DATA_DIR": ".mash", "MASH_DATABASE_URL": ""},
                    clear=False,
                ):
                    spec = MasherAgentSpec()
                    self.assertEqual(
                        spec.runtime_context.require_trace_digest_jsonl_path(),
                        (Path(tmp) / ".mash" / "masher" / "trace-digests.jsonl").resolve(),
                    )
                    self.assertEqual(
                        spec.runtime_context.require_online_eval_jsonl_path(),
                        (Path(tmp) / ".mash" / "masher" / "online-evals.jsonl").resolve(),
                    )
            finally:
                os.chdir(previous_cwd)

    def test_masher_skill_files_are_package_resources(self) -> None:
        masher_root = importlib.resources.files("mash.agents.masher")
        trace_skill = masher_root / "skills" / "trace-digest-workflow" / "SKILL.md"
        online_eval_skill = masher_root / "skills" / "online-eval-curation" / "SKILL.md"

        self.assertTrue(trace_skill.is_file())
        self.assertTrue(online_eval_skill.is_file())
        trace_text = trace_skill.read_text(encoding="utf-8")
        online_eval_text = online_eval_skill.read_text(encoding="utf-8")
        self.assertIn("name: trace-digest-workflow", trace_text)
        self.assertIn("name: online-eval-curation", online_eval_text)
        for skill_text in (trace_text, online_eval_text):
            self.assertIn("Use the tool result as the workflow outcome.", skill_text)
            self.assertNotIn("Do not use a code fence.", skill_text)
            self.assertNotIn("raw JSON", skill_text)

    def test_trace_digest_workflow_trace_mode_returns_digest_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_store = self._build_runtime_store()
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.accepted",
                created_at=1.0,
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.llm.think.completed",
                created_at=2.0,
                loop_index=0,
                payload={
                    "duration_ms": 500,
                    "action_type": "response",
                    "token_usage": {"input": 10, "output": 4},
                },
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.step.completed",
                created_at=2.0,
                loop_index=0,
                payload={"duration_ms": 500},
            )
            self._save_trace_log(
                runtime_store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.completed",
                created_at=2.1,
            )
            artifact_path = Path(tmp) / "masher" / "trace-digests.jsonl"

            spec = self._build_masher_spec(
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
            self.assertEqual(digest["schema_version"], 2)
            self.assertEqual(digest["target_agent_id"], "primary")
            self.assertEqual(digest["session_id"], "s-1")
            self.assertEqual(digest["trace_id"], "t-1")
            self.assertEqual(digest["tokens"]["input_tokens"], 10)
            self.assertIn("timing", digest)
            self.assertIn("total_duration_ms", digest["timing"])
            self.assertIn("tool_stats", digest)
            self.assertIn("step_breakdown", digest)
            self.assertIn("slowest_operations", digest)
            self.assertIn("subagent_traces", digest)
            self.assertFalse(artifact_path.exists())

    def test_shared_trace_bundle_extracts_eval_fields(self) -> None:
        runtime_store = self._build_runtime_store()
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
            event_type="runtime.llm.think.completed",
            created_at=4.0,
            payload={"token_usage": {"input": 12, "output": 5}},
        )
        self._save_trace_log(
            runtime_store,
            session_id="s-1",
            trace_id="t-1",
            event_type="runtime.request.completed",
            created_at=5.0,
            payload={"response": {"text": "It works like this."}},
        )

        events = asyncio.run(
            _load_trace_events(
                runtime_store,
                target_agent_id="primary",
                session_id="s-1",
                trace_id="t-1",
            )
        )
        bundle = build_runtime_trace(events)
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)
        row = _build_online_eval_row(bundle, analysis)

        self.assertEqual(row["user_message"], "How should this work?")
        self.assertEqual(row["assistant_response"], "It works like this.")
        self.assertEqual(row["tools_called"], ["bash"])
        self.assertEqual(row["tool_call_count"], 1)
        self.assertEqual(row["step_count"], 1)
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 5)
        self.assertIn("timing", row)
        self.assertIn("total_duration_ms", row["timing"])

    def test_trace_digest_workflow_incremental_mode_writes_jsonl_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_store = self._build_runtime_store()
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

            spec = self._build_masher_spec(
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
            self.assertEqual(digest["schema_version"], 2)
            self.assertIn("timing", digest)
            self.assertIn("notable_events", digest)

    def test_online_eval_workflow_trace_mode_writes_dataset_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_store = self._build_runtime_store()
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

            spec = self._build_masher_spec(
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
            runtime_store = self._build_runtime_store()
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

            spec = self._build_masher_spec(
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
        runtime_store = self._build_runtime_store()
        spec = self._build_masher_spec(runtime_store=runtime_store)
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

    def test_builder_enables_masher_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ):
                host = HostBuilder().agent(self._primary_spec(), metadata=metadata()).build()
                try:
                    self.assertNotIn("masher", host.list_agents())
                    workflows = {
                        workflow.workflow_id
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertIn(MASHER_TRACE_DIGEST_WORKFLOW_ID, workflows)
                    self.assertIn(MASHER_ONLINE_EVAL_WORKFLOW_ID, workflows)
                finally:
                    asyncio.run(host.close())

    def test_builder_skips_masher_when_no_provider_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                host = HostBuilder().agent(self._primary_spec(), metadata=metadata()).build()
                try:
                    workflows = {
                        workflow.workflow_id
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertNotIn(MASHER_TRACE_DIGEST_WORKFLOW_ID, workflows)
                    self.assertNotIn(MASHER_ONLINE_EVAL_WORKFLOW_ID, workflows)
                finally:
                    asyncio.run(host.close())

    def test_builder_enable_masher_registers_hidden_workflow_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ):
                host = HostBuilder().agent(self._primary_spec(), metadata=metadata()).enable_masher().build()
                try:
                    described = {item["agent_id"]: item for item in host.describe_agents()}
                    self.assertNotIn("masher", described)
                    self.assertNotIn("masher", host.list_agents())
                    workflows = {
                        workflow.workflow_id: workflow
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertIn(MASHER_TRACE_DIGEST_WORKFLOW_ID, workflows)
                    self.assertIn(MASHER_ONLINE_EVAL_WORKFLOW_ID, workflows)
                    self.assertEqual(
                        workflows[
                            MASHER_TRACE_DIGEST_WORKFLOW_ID
                        ].tasks[0].structured_output,
                        MASHER_TRACE_DIGEST_STRUCTURED_OUTPUT,
                    )
                    self.assertEqual(
                        workflows[
                            MASHER_ONLINE_EVAL_WORKFLOW_ID
                        ].tasks[0].structured_output,
                        MASHER_ONLINE_EVAL_STRUCTURED_OUTPUT,
                    )
                finally:
                    asyncio.run(host.close())


if __name__ == "__main__":
    unittest.main()

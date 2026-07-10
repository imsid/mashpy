"""Tests for the built-in Masher worker and its v2 step pipelines."""

from __future__ import annotations

import asyncio
import importlib.resources
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast
from unittest.mock import patch

from pydantic import ValidationError

if TYPE_CHECKING:
    from mash.evals.service import EvalService
    from mash.runtime.events import RuntimeStore

from mash.agents import MasherAgentSpec
from mash.agents.masher import (
    GEN_SYNTHETIC_EVALS_SKILL_NAME,
    MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
    MASHER_ONLINE_EVAL_WORKFLOW_ID,
    MASHER_SCORE_EVALS_WORKFLOW_ID,
    MASHER_TRACE_DIGEST_WORKFLOW_ID,
    MasherRuntimeContext,
)
from mash.agents.masher.pipelines import (
    DatasetRow,
    GeneratedEval,
    Rubric,
    TraceScanInput,
    build_gen_synthetic_evals_workflow,
    build_online_eval_curation_workflow,
    build_trace_digest_workflow,
)
from mash.agents.masher.traces import (
    build_online_eval_row,
    load_trace_events,
)
from mash.core.agent import Agent
from mash.core.llm import LLMProvider, OSSCompatibleProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.runtime import AgentMetadata, AgentSpec, Host, HostBuilder
from mash.runtime.events import analyze_trace, build_runtime_trace, build_span_tree
from mash.runtime.events.types import RuntimeEvent
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec
from mash.workflows.strategy import WorkflowStrategy


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
        loop_index: int | None = None,
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
                loop_index=loop_index,
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


async def _run_code_pipeline(
    workflow: WorkflowSpec, workflow_input: dict[str, Any]
) -> dict[str, Any]:
    """Drive an all-code pipeline with the engine's merge/coerce semantics."""
    prev: dict[str, Any] = {}
    for step in workflow.steps:
        assert isinstance(step, CodeStep)
        merged = {**workflow_input, **prev}
        inp = step.input.model_validate(merged)
        ctx = StepContext(
            run_id="run-1",
            step_id=step.step_id,
            workflow_input=dict(workflow_input),
        )
        result = step.run(inp, ctx)
        if inspect.isawaitable(result):
            result = await result
        out = result if isinstance(result, step.output) else step.output.model_validate(result)
        prev = out.model_dump(mode="json")
    return prev


def _build_context(
    *,
    runtime_store: _FakeRuntimeStore | None = None,
    tmp: str | None = None,
) -> MasherRuntimeContext:
    context = MasherRuntimeContext()
    if runtime_store is not None:
        context.bind_runtime_store(cast("RuntimeStore", runtime_store))
    if tmp is not None:
        context.configure_artifacts(Path(tmp))
    return context


def _save_trace_log(
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
    store.append(
        app_id=app_id,
        session_id=session_id,
        trace_id=trace_id,
        event_type=event_type,
        created_at=created_at,
        payload=payload,
        loop_index=loop_index,
    )


class MasherSpecTests(unittest.TestCase):
    def _primary_spec(self) -> AgentSpec:
        return build_spec(agent_id="primary", response_text="primary-ok")

    def test_builder_enable_masher_false_leaves_builder_unchanged(self) -> None:
        host = (
            HostBuilder()
            .agent(self._primary_spec(), metadata=metadata())
            .enable_masher(False)
            .build()
        )
        try:
            described = {str(item["agent_id"]): item for item in host.describe_agents()}
            self.assertEqual(sorted(described.keys()), ["primary"])
            self.assertEqual(host.get_workflow_registry().list(), [])
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

    def test_spec_registers_no_tools_and_only_generation_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = MasherAgentSpec()

                tools = spec.build_tools()
                skills = spec.build_skills()

                # The deterministic work moved into workflow code steps;
                # Masher itself only generates and judges via structured
                # output, so it has no workflow tools left.
                self.assertEqual(tools.list_tools(), [])
                agent = Agent(
                    llm=_FakeLLMProvider(),
                    tools=tools,
                    skills=skills,
                    config=spec.build_agent_config(),
                )
                self.assertEqual(sorted(agent.tools.list_tools()), ["Skill"])
                self.assertEqual(
                    [skill.name for skill in skills.list_skills()],
                    [GEN_SYNTHETIC_EVALS_SKILL_NAME],
                )
                prompt = spec.build_agent_config().system_prompt
                self.assertIn("workflow_input", prompt)
                self.assertIn("skill_name", prompt)
                self.assertIn("score-evals", prompt)
                self.assertIn("structured output", prompt)
                self.assertNotIn("trace-digest", prompt)
                self.assertNotIn("online-eval-curation", prompt)

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

    def test_generation_skill_is_a_package_resource(self) -> None:
        skills_root = importlib.resources.files("mash.agents.masher") / "skills"
        gen_skill = skills_root / "gen-synthetic-evals" / "SKILL.md"
        self.assertTrue(gen_skill.is_file())
        gen_text = gen_skill.read_text(encoding="utf-8")
        self.assertIn("name: gen-synthetic-evals", gen_text)
        self.assertIn("step id `generate`", gen_text)
        self.assertNotIn("run_gen_synthetic_evals_workflow", gen_text)
        # The trace workflows are all code now; their skills are gone.
        self.assertFalse((skills_root / "trace-digest-workflow").is_dir())
        self.assertFalse((skills_root / "online-eval-curation").is_dir())


class TraceDigestPipelineTests(unittest.TestCase):
    def test_trace_mode_returns_digest_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeRuntimeStore()
            _save_trace_log(
                store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.accepted",
                created_at=1.0,
            )
            _save_trace_log(
                store,
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
            _save_trace_log(
                store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.step.completed",
                created_at=2.0,
                loop_index=0,
                payload={"duration_ms": 500},
            )
            _save_trace_log(
                store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.completed",
                created_at=2.1,
            )
            context = _build_context(runtime_store=store, tmp=tmp)
            workflow = build_trace_digest_workflow(context)

            result = asyncio.run(
                _run_code_pipeline(
                    workflow,
                    {
                        "mode": "trace",
                        "target_agent_id": "primary",
                        "session_id": "s-1",
                        "trace_id": "t-1",
                    },
                )
            )

            self.assertEqual(result["mode"], "trace")
            self.assertEqual(result["processed_trace_count"], 1)
            self.assertEqual(result["appended_trace_count"], 0)
            self.assertIsNone(result["artifact_path"])
            digest = result["digest"]
            self.assertEqual(digest["schema_version"], 2)
            self.assertEqual(digest["target_agent_id"], "primary")
            self.assertEqual(digest["session_id"], "s-1")
            self.assertEqual(digest["trace_id"], "t-1")
            self.assertEqual(digest["tokens"]["input_tokens"], 10)
            self.assertIn("total_duration_ms", digest["timing"])
            for key in ("tool_stats", "step_breakdown", "slowest_operations", "subagent_traces"):
                self.assertIn(key, digest)
            self.assertFalse(context.require_trace_digest_jsonl_path().exists())

    def test_batch_mode_writes_jsonl_and_reports_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeRuntimeStore()
            _save_trace_log(store, session_id="s-old", trace_id="t-old", created_at=1.0)
            _save_trace_log(store, session_id="s-new", trace_id="t-new", created_at=3.0)
            _save_trace_log(
                store,
                session_id="s-new",
                trace_id="t-new",
                event_type="agent.tool.error",
                created_at=4.0,
                payload={"error": "boom"},
            )
            context = _build_context(runtime_store=store, tmp=tmp)
            workflow = build_trace_digest_workflow(context)

            result = asyncio.run(
                _run_code_pipeline(
                    workflow,
                    {"mode": "batch", "target_agent_id": "primary", "since_ts": 2.0},
                )
            )

            self.assertEqual(result["schema_version"], 3)
            self.assertEqual(result["processed_trace_count"], 1)
            self.assertEqual(result["appended_trace_count"], 1)
            # The watermark for the caller's next since_ts.
            self.assertEqual(result["latest_event_at"], 4.0)
            artifact_path = context.require_trace_digest_jsonl_path()
            lines = artifact_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            digest = json.loads(lines[0])
            self.assertEqual(digest["trace_id"], "t-new")
            self.assertIn("notable_events", digest)

    def test_batch_mode_skips_already_appended_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeRuntimeStore()
            _save_trace_log(store, session_id="s-1", trace_id="t-1", created_at=3.0)
            context = _build_context(runtime_store=store, tmp=tmp)
            workflow = build_trace_digest_workflow(context)
            workflow_input = {"mode": "batch", "target_agent_id": "primary"}

            first = asyncio.run(_run_code_pipeline(workflow, workflow_input))
            second = asyncio.run(_run_code_pipeline(workflow, workflow_input))

            self.assertEqual(first["appended_trace_count"], 1)
            self.assertEqual(second["appended_trace_count"], 0)
            artifact_path = context.require_trace_digest_jsonl_path()
            self.assertEqual(
                len(artifact_path.read_text(encoding="utf-8").splitlines()), 1
            )

    def test_trace_mode_requires_session_and_trace_ids(self) -> None:
        with self.assertRaises(ValidationError):
            TraceScanInput.model_validate(
                {"mode": "trace", "target_agent_id": "primary", "session_id": "s-1"}
            )

    def test_trace_mode_fails_when_trace_has_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = _build_context(runtime_store=_FakeRuntimeStore(), tmp=tmp)
            workflow = build_trace_digest_workflow(context)
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    _run_code_pipeline(
                        workflow,
                        {
                            "mode": "trace",
                            "target_agent_id": "primary",
                            "session_id": "s-x",
                            "trace_id": "t-x",
                        },
                    )
                )


class OnlineEvalCurationPipelineTests(unittest.TestCase):
    def test_shared_trace_bundle_extracts_eval_fields(self) -> None:
        store = _FakeRuntimeStore()
        _save_trace_log(
            store,
            session_id="s-1",
            trace_id="t-1",
            event_type="agent.run.start",
            created_at=1.0,
            payload={"user_message": "How should this work?"},
        )
        _save_trace_log(
            store,
            session_id="s-1",
            trace_id="t-1",
            event_type="runtime.tool.call.completed",
            created_at=2.0,
            payload={"tool_name": "bash"},
        )
        _save_trace_log(
            store,
            session_id="s-1",
            trace_id="t-1",
            event_type="runtime.step.completed",
            created_at=3.0,
            payload={},
            loop_index=0,
        )
        _save_trace_log(
            store,
            session_id="s-1",
            trace_id="t-1",
            event_type="runtime.llm.think.completed",
            created_at=4.0,
            payload={"token_usage": {"input": 12, "output": 5}},
        )
        _save_trace_log(
            store,
            session_id="s-1",
            trace_id="t-1",
            event_type="runtime.request.completed",
            created_at=5.0,
            payload={"response": {"text": "It works like this."}},
        )

        events = asyncio.run(
            load_trace_events(
                cast("RuntimeStore", store),
                target_agent_id="primary",
                session_id="s-1",
                trace_id="t-1",
            )
        )
        bundle = build_runtime_trace(events)
        analysis = analyze_trace(build_span_tree(events))
        row = build_online_eval_row(bundle, analysis)

        self.assertEqual(row["user_message"], "How should this work?")
        self.assertEqual(row["assistant_response"], "It works like this.")
        self.assertEqual(row["tools_called"], ["bash"])
        self.assertEqual(row["tool_call_count"], 1)
        self.assertEqual(row["step_count"], 1)
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 5)
        self.assertIn("total_duration_ms", row["timing"])

    def test_trace_mode_appends_row_and_returns_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeRuntimeStore()
            _save_trace_log(
                store,
                session_id="s-1",
                trace_id="t-1",
                event_type="agent.run.start",
                created_at=1.0,
                payload={"user_message": "Question"},
            )
            _save_trace_log(
                store,
                session_id="s-1",
                trace_id="t-1",
                event_type="runtime.request.completed",
                created_at=2.0,
                payload={"response": {"text": "Answer"}},
            )
            context = _build_context(runtime_store=store, tmp=tmp)
            workflow = build_online_eval_curation_workflow(context)

            result = asyncio.run(
                _run_code_pipeline(
                    workflow,
                    {
                        "mode": "trace",
                        "target_agent_id": "primary",
                        "session_id": "s-1",
                        "trace_id": "t-1",
                    },
                )
            )

            self.assertEqual(result["appended_trace_count"], 1)
            self.assertEqual(result["record"]["user_message"], "Question")
            self.assertEqual(result["record"]["assistant_response"], "Answer")
            self.assertNotIn("summary", result["record"])
            artifact_path = context.require_online_eval_jsonl_path()
            lines = artifact_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["trace_id"], "t-1")

    def test_batch_mode_skips_duplicate_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeRuntimeStore()
            _save_trace_log(
                store,
                session_id="s-new",
                trace_id="t-new",
                event_type="agent.run.start",
                created_at=3.0,
                payload={"user_message": "Question"},
            )
            _save_trace_log(
                store,
                session_id="s-new",
                trace_id="t-new",
                event_type="runtime.request.completed",
                created_at=4.0,
                payload={"response": {"text": "Answer"}},
            )
            context = _build_context(runtime_store=store, tmp=tmp)
            artifact_path = context.require_online_eval_jsonl_path()
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
            workflow = build_online_eval_curation_workflow(context)

            result = asyncio.run(
                _run_code_pipeline(
                    workflow,
                    {"mode": "batch", "target_agent_id": "primary", "since_ts": 2.0},
                )
            )

            self.assertEqual(result["processed_trace_count"], 1)
            self.assertEqual(result["appended_trace_count"], 0)
            self.assertEqual(
                len(artifact_path.read_text(encoding="utf-8").splitlines()), 1
            )


class _FakeEval:
    eval_id = "eval_1"
    host_id = "guide"
    dataset_id = "ds_1"
    rubric_id = "rbr_1"


class _FakeEvalService:
    def __init__(self) -> None:
        self.persisted_kwargs: dict[str, Any] | None = None

    async def persist_eval(self, **kwargs: Any) -> _FakeEval:
        self.persisted_kwargs = kwargs
        return _FakeEval()


class _FakePool:
    def __init__(self) -> None:
        self._host = Host(host_id="guide", primary="pilot", subagents=("helper",))
        self._metadata = {
            "pilot": AgentMetadata(
                display_name="Pilot",
                description="Primary guide agent.",
                capabilities=["answer questions", "delegate work"],
                usage_guidance="Default entry point.",
            ),
            "helper": AgentMetadata(
                display_name="Helper",
                description="Subagent for lookups.",
                capabilities=["reference lookup"],
                usage_guidance="Delegate lookups here.",
            ),
        }

    def get_host(self, host_id: str) -> Host:
        if host_id != self._host.host_id:
            raise ValueError(f"host '{host_id}' is not defined")
        return self._host

    def get_agent_metadata(self, agent_id: str):
        return self._metadata.get(agent_id)


def _gen_rows(count: int) -> list[dict[str, Any]]:
    return [
        {
            "input": f"question {index}",
            "scenario_description": "scenario",
            "sampling_category": "random",
            "expected_behavior": "answers correctly",
            "target_agents": ["pilot"],
        }
        for index in range(count)
    ]


_GEN_RUBRIC = {
    "global_scoring_prompt": "Judge it.",
    "criteria": [
        {"name": "accuracy", "description": "d", "weight": 1.0, "scoring_prompt": "p"},
    ],
}


class GenSyntheticEvalsPipelineTests(unittest.TestCase):
    def _workflow(self) -> tuple[WorkflowSpec, MasherRuntimeContext, _FakeEvalService]:
        spec = MasherAgentSpec()
        service = _FakeEvalService()
        spec.runtime_context.bind_pool(_FakePool())
        spec.runtime_context.bind_eval_service(cast("EvalService", service))
        return build_gen_synthetic_evals_workflow(spec), spec.runtime_context, service

    def test_pipeline_shape_and_generation_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                workflow, _, _ = self._workflow()
                self.assertEqual(
                    [(step.step_id, step.kind) for step in workflow.steps],
                    [
                        ("profile-host", "code"),
                        ("generate", "agent"),
                        ("persist-eval", "code"),
                    ],
                )
                generate = workflow.steps[1]
                assert isinstance(generate, AgentStep)
                self.assertEqual(generate.agent_id, "masher")
                self.assertEqual(generate.skill_name, GEN_SYNTHETIC_EVALS_SKILL_NAME)
                self.assertIs(generate.output, GeneratedEval)

    def test_profile_host_step_collects_declared_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                workflow, _, _ = self._workflow()
                profile_step = workflow.steps[0]
                assert isinstance(profile_step, CodeStep)
                inp = profile_step.input.model_validate({"host_id": "guide"})
                ctx = StepContext(
                    run_id="run-1", step_id="profile-host", workflow_input={}
                )

                profile = profile_step.run(inp, ctx)

                self.assertEqual(profile.primary_agent_id, "pilot")
                self.assertEqual(
                    [(p.agent_id, p.role) for p in profile.agent_profiles],
                    [("pilot", "primary"), ("helper", "subagent")],
                )
                self.assertEqual(
                    profile.agent_profiles[0].capabilities,
                    ["answer questions", "delegate work"],
                )

    def test_persist_step_persists_generated_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                workflow, _, service = self._workflow()
                persist_step = workflow.steps[2]
                assert isinstance(persist_step, CodeStep)
                inp = persist_step.input.model_validate(
                    {
                        "host_id": "guide",
                        "row_count": 5,
                        "dataset_rows": _gen_rows(5),
                        "rubric": _GEN_RUBRIC,
                    }
                )
                ctx = StepContext(
                    run_id="run-1", step_id="persist-eval", workflow_input={}
                )

                result = asyncio.run(persist_step.run(inp, ctx))

                self.assertEqual(result.eval_id, "eval_1")
                self.assertEqual(result.row_count, 5)
                persisted = service.persisted_kwargs
                assert persisted is not None
                self.assertEqual(len(persisted["dataset_rows"]), 5)
                self.assertEqual(persisted["rubric"]["criteria"][0]["weight"], 1.0)

    def test_persist_step_rejects_row_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                workflow, _, service = self._workflow()
                persist_step = workflow.steps[2]
                assert isinstance(persist_step, CodeStep)
                inp = persist_step.input.model_validate(
                    {
                        "host_id": "guide",
                        "row_count": 5,
                        "dataset_rows": _gen_rows(4),
                        "rubric": _GEN_RUBRIC,
                    }
                )
                ctx = StepContext(
                    run_id="run-1", step_id="persist-eval", workflow_input={}
                )

                with self.assertRaises(ValueError):
                    asyncio.run(persist_step.run(inp, ctx))
                self.assertIsNone(service.persisted_kwargs)

    def test_generated_eval_validates_rows_and_rubric(self) -> None:
        with self.assertRaises(ValidationError):
            DatasetRow.model_validate(
                {
                    "input": "q",
                    "scenario_description": "s",
                    "expected_behavior": "e",
                    "sampling_category": "not-a-category",
                }
            )
        with self.assertRaises(ValidationError):
            Rubric.model_validate(
                {
                    "criteria": [
                        {
                            "name": "a",
                            "description": "d",
                            "scoring_prompt": "p",
                            "weight": 0.5,
                        }
                    ]
                }
            )
        with self.assertRaises(ValidationError):
            GeneratedEval.model_validate(
                {"dataset_rows": [], "rubric": _GEN_RUBRIC}
            )


class MasherBuilderTests(unittest.TestCase):
    def _primary_spec(self) -> AgentSpec:
        return build_spec(agent_id="primary", response_text="primary-ok")

    def test_builder_registers_all_workflows_with_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ):
                host = (
                    HostBuilder()
                    .agent(self._primary_spec(), metadata=metadata())
                    .build()
                )
                try:
                    # The worker agent stays hidden from public listings.
                    described = {item["agent_id"] for item in host.describe_agents()}
                    self.assertNotIn("masher", described)
                    self.assertNotIn("masher", host.list_agents())
                    workflows = {
                        workflow.workflow_id: workflow
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertEqual(
                        set(workflows),
                        {
                            MASHER_TRACE_DIGEST_WORKFLOW_ID,
                            MASHER_ONLINE_EVAL_WORKFLOW_ID,
                            MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                            MASHER_SCORE_EVALS_WORKFLOW_ID,
                        },
                    )
                    digest = workflows[MASHER_TRACE_DIGEST_WORKFLOW_ID]
                    self.assertEqual([step.kind for step in digest.steps], ["code"] * 3)
                    curation = workflows[MASHER_ONLINE_EVAL_WORKFLOW_ID]
                    self.assertEqual([step.kind for step in curation.steps], ["code"] * 3)
                    gen = workflows[MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID]
                    self.assertEqual(
                        [step.kind for step in gen.steps], ["code", "agent", "code"]
                    )
                finally:
                    asyncio.run(host.close())

    def test_builder_keyless_registers_only_code_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                host = (
                    HostBuilder()
                    .agent(self._primary_spec(), metadata=metadata())
                    .build()
                )
                try:
                    self.assertNotIn("masher", host.list_agents())
                    self.assertIsNone(host.get_registered_agent_spec("masher"))
                    workflows = {
                        workflow.workflow_id
                        for workflow in host.get_workflow_registry().list()
                    }
                    # The all-code pipelines run without an LLM, so a keyless
                    # deployment still gets them; the agent-step workflows are
                    # skipped.
                    self.assertEqual(
                        workflows,
                        {
                            MASHER_TRACE_DIGEST_WORKFLOW_ID,
                            MASHER_ONLINE_EVAL_WORKFLOW_ID,
                        },
                    )
                finally:
                    asyncio.run(host.close())

    def test_builder_attaches_masher_workflows_to_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "ANTHROPIC_API_KEY": "test-key"},
                clear=True,
            ):
                explicit = WorkflowSpec(
                    workflow_id="custom-chain",
                    strategy=_NoopStrategy(),
                )
                pool = (
                    HostBuilder()
                    .agent(self._primary_spec(), metadata=metadata())
                    .workflow(explicit)
                    .host(
                        Host(
                            host_id="main",
                            primary="primary",
                            workflows=("custom-chain",),
                        )
                    )
                    .build()
                )
                try:
                    attached = pool.get_host("main").workflows
                    # Explicit attachments come first and are preserved.
                    self.assertEqual(attached[0], "custom-chain")
                    for workflow_id in (
                        MASHER_TRACE_DIGEST_WORKFLOW_ID,
                        MASHER_ONLINE_EVAL_WORKFLOW_ID,
                        MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                        MASHER_SCORE_EVALS_WORKFLOW_ID,
                    ):
                        self.assertIn(workflow_id, attached)
                    self.assertEqual(len(attached), len(set(attached)))
                finally:
                    asyncio.run(pool.close())

    def test_builder_keyless_attaches_code_workflows_to_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                pool = (
                    HostBuilder()
                    .agent(self._primary_spec(), metadata=metadata())
                    .host(Host(host_id="main", primary="primary"))
                    .build()
                )
                try:
                    self.assertEqual(
                        set(pool.get_host("main").workflows),
                        {
                            MASHER_TRACE_DIGEST_WORKFLOW_ID,
                            MASHER_ONLINE_EVAL_WORKFLOW_ID,
                        },
                    )
                finally:
                    asyncio.run(pool.close())


class _NoopStrategy(WorkflowStrategy):
    async def run(self, ctx: Any) -> dict[str, Any]:
        del ctx
        return {}


if __name__ == "__main__":
    unittest.main()

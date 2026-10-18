"""Tests for the built-in Masher worker and its step pipelines."""

from __future__ import annotations

import asyncio
import importlib.resources
import os
import tempfile
import unittest
from typing import TYPE_CHECKING, Any, Optional, cast
from unittest.mock import patch

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from mash.evals.service import EvalService

from mash.agents import EvalAgentSpec, EvalJudgeAgentSpec
from mash.agents.masher import (
    GEN_SYNTHETIC_EVALS_SKILL_NAME,
    MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
    MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
    MasherRuntimeContext,
)
from mash.agents.masher.workflows import (
    DatasetRow,
    GeneratedEval,
    Rubric,
    build_gen_synthetic_evals_workflow,
)
from mash.core.agent import Agent
from mash.core.llm import LLMProvider, OSSCompatibleProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.runtime import AgentMetadata, AgentSpec, Host, HostBuilder
from mash.runtime.structured_output import serialize_structured_output
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec


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


class MasherSpecTests(unittest.TestCase):
    def _primary_spec(self) -> AgentSpec:
        return build_spec(agent_id="primary", response_text="primary-ok")

    def test_builder_always_registers_masher_and_its_workflows(self) -> None:
        host = (
            HostBuilder()
            .agent(self._primary_spec(), metadata=metadata())
            .build()
        )
        try:
            described = {str(item["agent_id"]): item for item in host.describe_agents()}
            self.assertEqual(
                sorted(described.keys()), ["eval-agent", "eval-judge-agent", "primary"]
            )
            self.assertEqual(
                {item.workflow_id for item in host.get_workflow_registry().list()},
                {
                    MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                    MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                },
            )
        finally:
            asyncio.run(host.close())

    def test_build_llm_falls_back_to_oss_when_base_url_and_model_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "MASH_DATA_DIR": tmp,
                    "OSS_BASE_URL": "http://gpu-box:8000/v1",
                    "EVAL_AGENT_OSS_MODEL": "Qwen/Qwen3-32B",
                },
                clear=True,
            ):
                provider = EvalAgentSpec().build_llm()
                self.assertIsInstance(provider, OSSCompatibleProvider)

    def test_build_llm_raises_when_oss_base_url_set_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "OSS_BASE_URL": "http://gpu-box:8000/v1"},
                clear=True,
            ):
                with self.assertRaises(RuntimeError):
                    EvalAgentSpec().build_llm()

    def test_build_llm_raises_without_any_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                with self.assertRaises(RuntimeError):
                    EvalAgentSpec().build_llm()

    def test_spec_registers_no_tools_and_only_generation_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=False):
                spec = EvalAgentSpec()

                tools = spec.build_tools()
                skills = spec.build_skills()

                # The deterministic work moved into workflow code steps;
                # Generation uses structured output and no workflow tools.
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
                self.assertIn("structured output", prompt)

    def test_judge_spec_is_toolless_skillless_and_focused(self) -> None:
        spec = EvalJudgeAgentSpec()
        self.assertEqual(spec.build_tools().list_tools(), [])
        self.assertEqual(spec.build_skills().list_skills(), [])
        prompt = spec.build_agent_config().system_prompt
        self.assertIn("run-experiment", prompt)
        self.assertIn("rubric", prompt)
        self.assertNotIn("skill_name", prompt)

    def test_generation_skill_is_a_package_resource(self) -> None:
        skills_root = importlib.resources.files("mash.agents.masher") / "skills"
        gen_skill = skills_root / "gen-synthetic-evals" / "SKILL.md"
        self.assertTrue(gen_skill.is_file())
        gen_text = gen_skill.read_text(encoding="utf-8")
        self.assertIn("name: gen-synthetic-evals", gen_text)
        self.assertIn("step id `generate`", gen_text)
        self.assertNotIn("run_gen_synthetic_evals_workflow", gen_text)


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
        spec = EvalAgentSpec()
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
                self.assertEqual(generate.agent_id, "eval-agent")
                self.assertEqual(generate.skill_name, GEN_SYNTHETIC_EVALS_SKILL_NAME)
                self.assertIs(generate.output, GeneratedEval)

    def test_generated_eval_schema_is_valid_for_strict_structured_output(self) -> None:
        schema = serialize_structured_output(GeneratedEval)
        assert schema is not None
        dataset_row = schema["$defs"]["DatasetRow"]
        rubric = schema["$defs"]["Rubric"]
        criterion = schema["$defs"]["RubricCriterion"]

        self.assertEqual(
            set(dataset_row["required"]), set(dataset_row["properties"])
        )
        self.assertEqual(set(rubric["required"]), set(rubric["properties"]))
        self.assertEqual(
            set(criterion["required"]), set(criterion["properties"])
        )
        self.assertNotIn("default", rubric["properties"]["global_scoring_prompt"])
        self.assertNotIn("default", criterion["properties"]["scale_min"])
        self.assertNotIn("default", criterion["properties"]["scale_max"])

    def test_generated_eval_schema_has_no_dataset_rows_max_items(self) -> None:
        # Gemini rejects maxItems >= 30 on an array of objects; row_count is
        # bounded on the workflow input and enforced exactly by persist-eval.
        schema = serialize_structured_output(GeneratedEval)
        assert schema is not None
        self.assertNotIn("maxItems", schema["properties"]["dataset_rows"])

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
                    described = {item["agent_id"] for item in host.describe_agents()}
                    self.assertIn("eval-agent", described)
                    self.assertIn("eval-judge-agent", described)
                    self.assertIn("eval-agent", host.list_agents())
                    self.assertIsNotNone(host.get_agent_metadata("eval-agent"))
                    workflows = {
                        workflow.workflow_id: workflow
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertEqual(
                        set(workflows),
                        {
                            MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                            MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                        },
                    )
                    gen = workflows[MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID]
                    self.assertEqual(
                        [step.kind for step in gen.steps], ["code", "agent", "code"]
                    )
                    run_experiment = workflows[MASHER_RUN_EXPERIMENT_WORKFLOW_ID]
                    self.assertEqual(
                        [step.kind for step in run_experiment.steps], ["code"] * 3
                    )
                finally:
                    asyncio.run(host.close())

    def test_builder_keyless_still_registers_masher_and_all_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}, clear=True):
                host = (
                    HostBuilder()
                    .agent(self._primary_spec(), metadata=metadata())
                    .build()
                )
                try:
                    self.assertIn("eval-agent", host.list_agents())
                    self.assertIn("eval-judge-agent", host.list_agents())
                    self.assertIsNotNone(host.get_registered_agent_spec("eval-agent"))
                    workflows = {
                        workflow.workflow_id
                        for workflow in host.get_workflow_registry().list()
                    }
                    self.assertEqual(
                        workflows,
                        {
                            MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                            MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
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
                    steps=[
                        CodeStep(
                            step_id="noop",
                            run=_noop_step,
                            input=_NoopModel,
                            output=_NoopModel,
                        )
                    ],
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
                        MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                        MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                    ):
                        self.assertIn(workflow_id, attached)
                    self.assertEqual(len(attached), len(set(attached)))
                finally:
                    asyncio.run(pool.close())

    def test_builder_keyless_attaches_all_workflows_to_hosts(self) -> None:
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
                            MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
                            MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                        },
                    )
                finally:
                    asyncio.run(pool.close())


class _NoopModel(BaseModel):
    pass


def _noop_step(inp: _NoopModel, _ctx: StepContext) -> _NoopModel:
    return inp


if __name__ == "__main__":
    unittest.main()

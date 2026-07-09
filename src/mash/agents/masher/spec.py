"""Built-in Masher workflow worker spec."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mash.core.config import AgentConfig
from mash.core.llm import (
    DEFAULT_GEMINI_MODEL,
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OpenAIProvider,
    OSSCompatibleProvider,
)
from mash.runtime.host.subagents import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry
from mash.workflows import WorkflowSpec

from .context import MasherRuntimeContext
from .pipelines import (
    MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
    MASHER_ONLINE_EVAL_WORKFLOW_ID,
    MASHER_TRACE_DIGEST_WORKFLOW_ID,
    build_gen_synthetic_evals_workflow,
    build_online_eval_curation_workflow,
    build_trace_digest_workflow,
)
from .score_runner import ScoreEvalsStrategy

if TYPE_CHECKING:
    from mash.runtime.service import AgentRuntime

MASHER_AGENT_ID = "masher"
MASHER_SCORE_EVALS_WORKFLOW_ID = "score-evals"


def _select_masher_provider_kind() -> str | None:
    """Which provider family build_llm() dispatches on, or None if unconfigured.

    Single source of truth shared by build_llm() and provider_available() so the
    two cannot drift. OSS is selected on OSS_BASE_URL alone; build_llm() then
    enforces MASHER_OSS_MODEL.
    """
    if (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    ):
        return "gemini"
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.getenv("OSS_BASE_URL", "").strip():
        return "oss"
    return None


_PROMPT = """You are Masher, Mash's built-in workflow worker and eval judge.

You are invoked only by Mash workflows. Do not answer free-form chat.

Workflow step requests are JSON with workflow_id, workflow_run_id, step_id,
workflow_input, and input. The `input` object carries everything the step
needs; each run is a clean slate — there is no cross-run state. When the
request names a skill_name, call the standard Skill tool with it exactly once
before doing the step's work, then follow the loaded skill.

Judge requests (from the score-evals workflow) are self-contained scoring
messages carrying a rubric and an output to evaluate; follow the message's
scoring instructions exactly.

Always answer with the structured output the request demands.
"""


def build_masher_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Masher",
        description="Workflow-only eval generation and judging worker.",
        capabilities=[
            "synthetic eval dataset and rubric generation",
            "rubric-based eval output judging",
        ],
        usage_guidance=(
            "Masher is registered by HostBuilder.enable_masher() as an internal "
            "workflow worker for the gen-synthetic-evals and score-evals "
            "workflows. It should not be exposed as a user-invokable subagent."
        ),
    )


class MasherAgentSpec(AgentSpec):
    """Built-in eval generation and judging worker."""

    def __init__(self) -> None:
        self.runtime_context = MasherRuntimeContext()
        self.runtime_context.configure_artifacts(AgentSpec.get_data_root())

    def get_agent_id(self) -> str:
        return MASHER_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        # Generation and judging are pure structured-output tasks; the
        # deterministic work (trace analysis, artifact writes, eval
        # persistence) lives in workflow code steps, not agent tools.
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skills_root = Path(__file__).resolve().parent / "skills"
        for skill in skills.get_custom_skills(skills_root):
            skills.register(skill)
        return skills

    @staticmethod
    def provider_available() -> bool:
        """Whether build_llm() would construct a provider from the environment.

        HostBuilder uses this to decide whether to register the Masher agent
        and its agent-step workflows: Masher cannot run without an LLM, so a
        keyless deployment skips them (the all-code workflows still register).
        True exactly when build_llm() succeeds — an OSS endpoint counts only
        once MASHER_OSS_MODEL is also set.
        """
        kind = _select_masher_provider_kind()
        if kind is None:
            return False
        if kind == "oss":
            return bool(os.getenv("MASHER_OSS_MODEL", "").strip())
        return True

    def build_llm(self) -> LLMProvider:
        kind = _select_masher_provider_kind()
        if kind == "gemini":
            return GeminiProvider(
                app_id=MASHER_AGENT_ID,
                model=os.getenv(
                    "MASHER_GEMINI_MODEL",
                    os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
                ),
            )
        if kind == "openai":
            return OpenAIProvider(
                app_id=MASHER_AGENT_ID,
                model=os.getenv(
                    "MASHER_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini")
                ),
            )
        if kind == "anthropic":
            return AnthropicProvider(
                app_id=MASHER_AGENT_ID,
                model=os.getenv(
                    "MASHER_ANTHROPIC_MODEL",
                    os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                ),
            )
        if kind == "oss":
            # Generic OSS endpoints have no universal model name, so the served
            # model must be named explicitly via MASHER_OSS_MODEL. It must also
            # support native tool calling for Masher to route correctly.
            oss_model = os.getenv("MASHER_OSS_MODEL", "").strip()
            if not oss_model:
                raise RuntimeError(
                    "Masher's OSS endpoint requires MASHER_OSS_MODEL to name the "
                    "served model (must support native tool calling)."
                )
            return OSSCompatibleProvider(
                app_id=MASHER_AGENT_ID,
                model=oss_model,
                base_url=os.getenv("OSS_BASE_URL", "").strip(),
                api_key=os.getenv("OSS_API_KEY", "").strip() or None,
            )
        raise RuntimeError(
            "Masher requires GEMINI_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY, "
            "ANTHROPIC_API_KEY, or OSS_BASE_URL (with MASHER_OSS_MODEL) to be "
            "configured."
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=MASHER_AGENT_ID,
            system_prompt=_PROMPT,
            max_steps=20,
        )

    def enable_runtime_tools(self) -> bool:
        return False

    def on_startup(self, runtime: "AgentRuntime") -> None:
        self.runtime_context.bind_runtime_store(runtime.runtime_store)


def build_masher_workflow_specs(
    masher_spec: MasherAgentSpec,
    *,
    include_agent_workflows: bool = True,
) -> list[WorkflowSpec]:
    """All of Masher's workflows.

    The trace-digest and online-eval-curation pipelines are all code and never
    touch an LLM, so they are always included — they run on keyless
    deployments. ``include_agent_workflows`` gates the two that need the Masher
    agent: gen-synthetic-evals (generation step) and score-evals (per-row
    judge).
    """
    context = masher_spec.runtime_context
    workflows = [
        build_trace_digest_workflow(context),
        build_online_eval_curation_workflow(context),
    ]
    if include_agent_workflows:
        workflows.append(build_gen_synthetic_evals_workflow(masher_spec))
        workflows.append(
            WorkflowSpec(
                workflow_id=MASHER_SCORE_EVALS_WORKFLOW_ID,
                strategy=ScoreEvalsStrategy(context=context),
            )
        )
    return workflows


def create_masher_agent_spec() -> MasherAgentSpec:
    """Build a spawnable Masher spec for child runtime processes."""
    return MasherAgentSpec()


__all__ = [
    "MASHER_AGENT_ID",
    "MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID",
    "MASHER_ONLINE_EVAL_WORKFLOW_ID",
    "MASHER_SCORE_EVALS_WORKFLOW_ID",
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "MasherAgentSpec",
    "build_masher_workflow_specs",
    "build_masher_metadata",
    "create_masher_agent_spec",
]

"""Agent specifications for synthetic eval generation and judging."""

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

from .context import MasherRuntimeContext

if TYPE_CHECKING:
    from mash.runtime.service import AgentRuntime

EVAL_AGENT_ID = "eval-agent"
EVAL_JUDGE_AGENT_ID = "eval-judge-agent"


def _select_eval_agent_provider_kind() -> str | None:
    """Which provider family build_llm() dispatches on, or None if unconfigured."""
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


_GENERATOR_PROMPT = """You are Mash's built-in synthetic eval generator.

You are invoked only by Mash workflows. Do not answer free-form chat.

Workflow step requests are JSON with workflow_id, workflow_run_id, step_id,
workflow_input, and input. The `input` object carries everything the step
needs; each run is a clean slate — there is no cross-run state. When the
request names a skill_name, call the standard Skill tool with it exactly once
before doing the step's work, then follow the loaded skill.

Always answer with the structured output the request demands.
"""

_JUDGE_PROMPT = """You are Mash's built-in eval judge.

You are invoked only by the run-experiment workflow. Do not answer free-form
chat and do not attempt the task yourself. Judge the supplied agent input and
output only against the supplied rubric. Follow every criterion's integer
scale and scoring instructions exactly.

Always answer with the structured output the request demands.
"""


def build_eval_agent_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Eval Agent",
        description="Built-in synthetic eval dataset and rubric generator.",
        capabilities=["synthetic eval dataset and rubric generation"],
        usage_guidance=(
            "This agent powers the gen-synthetic-evals workflow. Inspect its spec "
            "to understand the generation step."
        ),
    )


def build_eval_judge_agent_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Eval Judge",
        description="Built-in rubric-based evaluator for experiment outputs.",
        capabilities=["rubric-based eval output judging"],
        usage_guidance=(
            "This agent is invoked by the run-experiment workflow to score one "
            "host output against an eval rubric."
        ),
    )


def _model_override(prefix: str, provider: str, fallback: str) -> str:
    return os.getenv(
        f"{prefix}_{provider}_MODEL",
        os.getenv(f"EVAL_AGENT_{provider}_MODEL", fallback),
    )


def _build_eval_llm(*, agent_id: str, prefix: str, label: str) -> LLMProvider:
    kind = _select_eval_agent_provider_kind()
    if kind == "gemini":
        return GeminiProvider(
            app_id=agent_id,
            model=_model_override(
                prefix, "GEMINI", os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
            ),
        )
    if kind == "openai":
        return OpenAIProvider(
            app_id=agent_id,
            model=_model_override(
                prefix, "OPENAI", os.getenv("OPENAI_MODEL", "gpt-5-mini")
            ),
        )
    if kind == "anthropic":
        return AnthropicProvider(
            app_id=agent_id,
            model=_model_override(
                prefix,
                "ANTHROPIC",
                os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            ),
        )
    if kind == "oss":
        oss_model = os.getenv(
            f"{prefix}_OSS_MODEL", os.getenv("EVAL_AGENT_OSS_MODEL", "")
        ).strip()
        if not oss_model:
            raise RuntimeError(
                f"The {label} OSS endpoint requires {prefix}_OSS_MODEL or "
                "EVAL_AGENT_OSS_MODEL to name the served model."
            )
        return OSSCompatibleProvider(
            app_id=agent_id,
            model=oss_model,
            base_url=os.getenv("OSS_BASE_URL", "").strip(),
            api_key=os.getenv("OSS_API_KEY", "").strip() or None,
        )
    raise RuntimeError(
        f"The {label} requires GEMINI_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY, "
        "ANTHROPIC_API_KEY, or OSS_BASE_URL to be configured."
    )


class EvalAgentSpec(AgentSpec):
    """Built-in eval generation and judging worker."""

    def __init__(self) -> None:
        self.runtime_context = MasherRuntimeContext()
        self.runtime_context.configure_artifacts(AgentSpec.get_data_root())

    def get_agent_id(self) -> str:
        return EVAL_AGENT_ID

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

    def build_llm(self) -> LLMProvider:
        return _build_eval_llm(
            agent_id=EVAL_AGENT_ID,
            prefix="EVAL_AGENT",
            label="eval agent",
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=EVAL_AGENT_ID,
            system_prompt=_GENERATOR_PROMPT,
            max_steps=20,
        )

    def enable_runtime_tools(self) -> bool:
        return False

    def on_startup(self, runtime: "AgentRuntime") -> None:
        self.runtime_context.bind_runtime_store(runtime.runtime_store)


class EvalJudgeAgentSpec(AgentSpec):
    """Tool-less worker that judges one experiment row against a rubric."""

    def get_agent_id(self) -> str:
        return EVAL_JUDGE_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _build_eval_llm(
            agent_id=EVAL_JUDGE_AGENT_ID,
            prefix="EVAL_JUDGE_AGENT",
            label="eval judge agent",
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=EVAL_JUDGE_AGENT_ID,
            system_prompt=_JUDGE_PROMPT,
            max_steps=4,
        )

    def enable_runtime_tools(self) -> bool:
        return False


__all__ = [
    "EVAL_AGENT_ID",
    "EVAL_JUDGE_AGENT_ID",
    "EvalAgentSpec",
    "EvalJudgeAgentSpec",
    "build_eval_agent_metadata",
    "build_eval_judge_agent_metadata",
]

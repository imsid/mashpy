"""Built-in Masher workflow worker spec."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider, OpenAIProvider
from mash.runtime.spec import AgentSpec
from mash.runtime.host.subagents import SubAgentMetadata
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry
from mash.workflows import TaskSpec, WorkflowSpec

from .tool import (
    MasherRuntimeContext,
    OnlineEvalCurationWorkflowTool,
    TraceDigestWorkflowTool,
)

if TYPE_CHECKING:
    from mash.runtime.service import AgentRuntime

MASHER_AGENT_ID = "masher"
MASHER_TRACE_DIGEST_WORKFLOW_ID = "masher-trace-digest"
MASHER_TRACE_DIGEST_TASK_ID = "digest-traces"
MASHER_ONLINE_EVAL_WORKFLOW_ID = "masher-online-eval-curation"
MASHER_ONLINE_EVAL_TASK_ID = "curate-online-evals"

_PROMPT = """You are Masher, Mash's built-in workflow-only worker.

You are invoked only by Mash workflows. Do not answer free-form diagnostic chat.
Every request is JSON with workflow_id, workflow_run_id, task_id, workflow_input,
and task_state.

Structured event schema:
- Common envelope fields: event_id, event_type, app_id, session_id, trace_id, payload, created_at
- Important event families:
  - runtime.* for request lifecycle and persisted execution milestones
  - agent.* for run lifecycle and tool actions
  - llm.* for model usage, latency, token counts, finish reasons
  - memory.search.* for retrieval/search activity
  - mcp.* for MCP tool/server operations
  - command.* for command lifecycle events

Workflow skill routing:
- workflow_id=masher-trace-digest, task_id=digest-traces -> skill=trace-digest-workflow
- workflow_id=masher-online-eval-curation, task_id=curate-online-evals -> skill=online-eval-curation

Routing rules:
- Match both workflow_id and task_id exactly.
- Call the standard Skill tool exactly once with the matched skill name before doing workflow work.
- After the skill loads, follow only the loaded skill's workflow instructions.
- Do not infer a skill from workflow_input, task_state, user wording, or partial id matches.
- If no route matches, return a JSON error object and do not call workflow tools.
"""


def build_masher_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Masher",
        description="Workflow-only Mash trace digest worker.",
        capabilities=[
            "trace digest generation",
            "incremental trace checkpointing",
            "trace digest JSONL artifacts",
        ],
        usage_guidance=(
            "Masher is registered by HostBuilder.enable_masher() as an internal "
            "workflow worker for the masher-trace-digest workflow. It should not "
            "be exposed as a user-invokable subagent."
        ),
    )


class MasherAgentSpec(AgentSpec):
    """Built-in trace diagnosis subagent."""

    def __init__(self) -> None:
        self.runtime_context = MasherRuntimeContext()
        self.runtime_context.configure_artifacts(AgentSpec.get_data_root())

    def get_agent_id(self) -> str:
        return MASHER_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(
            TraceDigestWorkflowTool(
                context=self.runtime_context,
            )
        )
        tools.register(
            OnlineEvalCurationWorkflowTool(
                context=self.runtime_context,
            )
        )
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skills_root = Path(__file__).resolve().parent / "skills"
        trace_digest_skill_dir = skills_root / "trace-digest-workflow"
        online_eval_skill_dir = skills_root / "online-eval-curation"
        skills.register(
            Skill(
                type="custom",
                name="trace-digest-workflow",
                description="Run Masher's diagnostic trace digest workflow.",
                location=str(trace_digest_skill_dir),
            )
        )
        skills.register(
            Skill(
                type="custom",
                name="online-eval-curation",
                description="Build normalized online eval JSONL rows from Mash trace events.",
                location=str(online_eval_skill_dir),
            )
        )
        return skills

    def build_llm(self) -> LLMProvider:
        if os.getenv("OPENAI_API_KEY", "").strip():
            return OpenAIProvider(
                app_id=MASHER_AGENT_ID,
                model=os.getenv(
                    "MASHER_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini")
                ),
            )
        if os.getenv("ANTHROPIC_API_KEY", "").strip():
            return AnthropicProvider(
                app_id=MASHER_AGENT_ID,
                model=os.getenv(
                    "MASHER_ANTHROPIC_MODEL",
                    os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                ),
            )
        raise RuntimeError(
            "Masher requires OPENAI_API_KEY or ANTHROPIC_API_KEY to be configured."
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=MASHER_AGENT_ID,
            system_prompt=_PROMPT,
            skills_enabled=True,
            max_steps=6,
        )

    def enable_runtime_tools(self) -> bool:
        return False

    def on_startup(self, runtime: "AgentRuntime") -> None:
        self.runtime_context.bind_runtime_store(runtime.runtime_store)


def build_masher_workflow_specs(masher_spec: MasherAgentSpec) -> list[WorkflowSpec]:
    return [
        WorkflowSpec(
            workflow_id=MASHER_TRACE_DIGEST_WORKFLOW_ID,
            tasks=[
                TaskSpec(
                    task_id=MASHER_TRACE_DIGEST_TASK_ID,
                    agent_spec=masher_spec,
                )
            ],
        ),
        WorkflowSpec(
            workflow_id=MASHER_ONLINE_EVAL_WORKFLOW_ID,
            tasks=[
                TaskSpec(
                    task_id=MASHER_ONLINE_EVAL_TASK_ID,
                    agent_spec=masher_spec,
                )
            ],
        ),
    ]


def create_masher_agent_spec(*, target_app_id: str | None = None) -> MasherAgentSpec:
    """Build a spawnable Masher spec for child runtime processes."""
    del target_app_id
    return MasherAgentSpec()


__all__ = [
    "MASHER_AGENT_ID",
    "MASHER_ONLINE_EVAL_TASK_ID",
    "MASHER_ONLINE_EVAL_WORKFLOW_ID",
    "MASHER_TRACE_DIGEST_TASK_ID",
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "MasherAgentSpec",
    "build_masher_workflow_specs",
    "build_masher_metadata",
    "create_masher_agent_spec",
]

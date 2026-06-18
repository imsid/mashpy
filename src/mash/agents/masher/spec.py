"""Built-in Masher workflow worker spec."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mash.core.config import AgentConfig
from mash.core.llm import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
    GeminiProvider,
    OSSCompatibleProvider,
    DEFAULT_GEMINI_MODEL,
)
from mash.runtime.host.subagents import AgentMetadata
from mash.runtime.spec import AgentSpec
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


def _select_masher_provider_kind() -> str | None:
    """Which provider family build_llm() dispatches on, or None if unconfigured.

    Single source of truth shared by build_llm() and provider_available() so the
    two cannot drift. OSS is selected on OSS_BASE_URL alone; build_llm() then
    enforces MASHER_OSS_MODEL.
    """
    if os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip():
        return "gemini"
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if os.getenv("OSS_BASE_URL", "").strip():
        return "oss"
    return None

MASHER_TRACE_DIGEST_STRUCTURED_OUTPUT = {
    "title": "MasherTraceDigestWorkflowOutput",
    "type": "object",
    "properties": {
        "json_text": {
            "type": "string",
            "description": "A serialized JSON object string containing the full trace digest output.",
        }
    },
    "required": ["json_text"],
    "additionalProperties": False,
}
MASHER_ONLINE_EVAL_STRUCTURED_OUTPUT = {
    "title": "MasherOnlineEvalCurationWorkflowOutput",
    "type": "object",
    "properties": {
        "json_text": {
            "type": "string",
            "description": "A serialized JSON object string containing the full trace digest output.",
        }
    },
    "required": ["json_text"],
    "additionalProperties": False,
}

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
- If no route matches, return an error object and do not call workflow tools.
"""


def build_masher_metadata() -> AgentMetadata:
    return AgentMetadata(
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

    @staticmethod
    def provider_available() -> bool:
        """Whether build_llm() would construct a provider from the environment.

        HostBuilder uses this to decide whether to register Masher by default:
        Masher cannot run without an LLM, so a keyless deployment skips it rather
        than failing at pool startup. True exactly when build_llm() succeeds — an
        OSS endpoint counts only once MASHER_OSS_MODEL is also set.
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
                    "MASHER_GEMINI_MODEL", os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
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
                    structured_output=MASHER_TRACE_DIGEST_STRUCTURED_OUTPUT,
                )
            ],
        ),
        WorkflowSpec(
            workflow_id=MASHER_ONLINE_EVAL_WORKFLOW_ID,
            tasks=[
                TaskSpec(
                    task_id=MASHER_ONLINE_EVAL_TASK_ID,
                    agent_spec=masher_spec,
                    structured_output=MASHER_ONLINE_EVAL_STRUCTURED_OUTPUT,
                )
            ],
        ),
    ]


def create_masher_agent_spec() -> MasherAgentSpec:
    """Build a spawnable Masher spec for child runtime processes."""
    return MasherAgentSpec()


__all__ = [
    "MASHER_AGENT_ID",
    "MASHER_ONLINE_EVAL_TASK_ID",
    "MASHER_ONLINE_EVAL_STRUCTURED_OUTPUT",
    "MASHER_ONLINE_EVAL_WORKFLOW_ID",
    "MASHER_TRACE_DIGEST_TASK_ID",
    "MASHER_TRACE_DIGEST_STRUCTURED_OUTPUT",
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "MasherAgentSpec",
    "build_masher_workflow_specs",
    "build_masher_metadata",
    "create_masher_agent_spec",
]

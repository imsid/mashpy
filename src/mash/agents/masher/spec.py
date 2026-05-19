"""Built-in Masher workflow worker spec."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider, OpenAIProvider
from mash.memory.store import MemoryStore
from mash.runtime.events import RuntimeStore
from mash.runtime.spec import AgentSpec
from mash.runtime.host.subagents import SubAgentMetadata
from mash.memory.store import PostgresStore, SQLiteStore
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.base import FunctionTool, ToolResult
from mash.tools.registry import ToolRegistry
from mash.workflows import TaskSpec, WorkflowSpec

from .tool import (
    AppendJsonlTool,
    GetLatestTraceTool,
    GetTraceEventsTool,
    ListTracesSinceTool,
    ListRecentTracesTool,
    OnlineEvalCurationWorkflowTool,
    TraceDigestWorkflowTool,
)

MASHER_AGENT_ID = "masher"
MASHER_TRACE_DIGEST_WORKFLOW_ID = "masher-trace-digest"
MASHER_TRACE_DIGEST_TASK_ID = "digest-traces"
MASHER_ONLINE_EVAL_WORKFLOW_ID = "masher-online-eval-curation"
MASHER_ONLINE_EVAL_TASK_ID = "curate-online-evals"

_PROMPT_HEADER_TEMPLATE = """You are Masher, Mash's built-in workflow-only worker.

Configured event store:
- {event_source}

Configured trace digest JSONL artifact:
- {trace_digest_jsonl_path}

Configured online eval JSONL artifact:
- {online_eval_jsonl_path}

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

Workflow skills:
{workflow_skills}
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

    def __init__(
        self,
        log_store: MemoryStore,
        *,
        target_app_id: str | None = None,
        runtime_store: RuntimeStore | None = None,
        runtime_database_url: str | None = None,
        trace_digest_jsonl_path: str | Path | None = None,
        online_eval_jsonl_path: str | Path | None = None,
    ) -> None:
        self.log_store = log_store
        self.target_app_id = (
            target_app_id.strip()
            if isinstance(target_app_id, str) and target_app_id.strip()
            else "primary"
        )
        self.runtime_store = runtime_store
        self.runtime_database_url = runtime_database_url
        self.store_path = (
            AgentSpec.get_data_root() / self.target_app_id / "state.db"
        ).resolve()
        self.trace_digest_jsonl_path = (
            Path(trace_digest_jsonl_path).expanduser().resolve()
            if trace_digest_jsonl_path is not None
            else (AgentSpec.get_data_root() / "masher" / "trace-digests.jsonl").resolve()
        )
        self.online_eval_jsonl_path = (
            Path(online_eval_jsonl_path).expanduser().resolve()
            if online_eval_jsonl_path is not None
            else (AgentSpec.get_data_root() / "masher" / "online-evals.jsonl").resolve()
        )

    def _build_target_store(self) -> MemoryStore:
        return self.log_store

    def get_agent_id(self) -> str:
        return MASHER_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        target_store = self._build_target_store()
        tools.register(self._build_get_latest_session_tool())
        tools.register(
            GetLatestTraceTool(
                target_store,
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                app_id=self.target_app_id,
            )
        )
        tools.register(
            ListRecentTracesTool(
                target_store,
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                app_id=self.target_app_id,
            )
        )
        tools.register(
            GetTraceEventsTool(
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                app_id=self.target_app_id,
            )
        )
        tools.register(
            ListTracesSinceTool(
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                app_id=self.target_app_id,
            )
        )
        tools.register(
            TraceDigestWorkflowTool(
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                default_target_agent_id=self.target_app_id,
                trace_digest_jsonl_path=self.trace_digest_jsonl_path,
            )
        )
        tools.register(
            OnlineEvalCurationWorkflowTool(
                runtime_store=self.runtime_store,
                runtime_database_url=self.runtime_database_url,
                default_target_agent_id=self.target_app_id,
                online_eval_jsonl_path=self.online_eval_jsonl_path,
            )
        )
        tools.register(BashTool(working_dir=str(self.store_path.parent)))
        tools.register(AppendJsonlTool())
        return tools

    def _build_get_latest_session_tool(self) -> FunctionTool:
        async def execute(_args: dict[str, object]) -> ToolResult:
            session = await self.log_store.get_latest_session(app_id=self.target_app_id)
            if session is None:
                return ToolResult.error("no sessions found for this app")
            return ToolResult.success(json.dumps(session, ensure_ascii=True, indent=2), **session)

        return FunctionTool(
            name="get_latest_session",
            description=(
                "Return the most recent session for the target app from the runtime "
                "store. Use this to resolve which session to inspect before fetching events."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

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
            system_prompt=_PROMPT_HEADER_TEMPLATE.format(
                event_source="runtime_event_log",
                trace_digest_jsonl_path=str(self.trace_digest_jsonl_path),
                online_eval_jsonl_path=str(self.online_eval_jsonl_path),
                workflow_skills=self._build_workflow_skill_prompt(),
            ),
            skills_enabled=True,
            max_steps=6,
        )

    def _build_workflow_skill_prompt(self) -> str:
        skills_root = Path(__file__).resolve().parent / "skills"
        rendered = [
            _render_skill(
                skills_root / "trace-digest-workflow" / "SKILL.md",
                {
                    "workflow_id": MASHER_TRACE_DIGEST_WORKFLOW_ID,
                    "task_id": MASHER_TRACE_DIGEST_TASK_ID,
                    "artifact_path": str(self.trace_digest_jsonl_path),
                    "tool_name": "run_trace_digest_workflow",
                },
            ),
            _render_skill(
                skills_root / "online-eval-curation" / "SKILL.md",
                {
                    "workflow_id": MASHER_ONLINE_EVAL_WORKFLOW_ID,
                    "task_id": MASHER_ONLINE_EVAL_TASK_ID,
                    "artifact_path": str(self.online_eval_jsonl_path),
                    "tool_name": "run_online_eval_curation_workflow",
                },
            ),
        ]
        return "\n\n".join(rendered)


def _render_skill(path: Path, values: dict[str, str]) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read Masher skill file: {path}") from exc
    body = _strip_frontmatter(content)
    for key, value in values.items():
        body = body.replace("{{" + key + "}}", value)
    if "{{" in body or "}}" in body:
        raise RuntimeError(f"unresolved placeholder in Masher skill file: {path}")
    return body.strip()


def _strip_frontmatter(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :])
    return content


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
    resolved_target = (
        target_app_id.strip()
        if isinstance(target_app_id, str) and target_app_id.strip()
        else "primary"
    )
    memory_database_url = os.getenv("MASH_MEMORY_DATABASE_URL", "").strip()
    if memory_database_url:
        store: MemoryStore = PostgresStore(memory_database_url)
    else:
        store_path = (AgentSpec.get_data_root() / resolved_target / "state.db").resolve()
        store = SQLiteStore(store_path)
    return MasherAgentSpec(
        store,
        target_app_id=resolved_target,
    )


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

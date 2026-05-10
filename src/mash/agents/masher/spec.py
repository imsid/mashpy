"""Built-in Masher subagent spec."""

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

from .tool import (
    AppendJsonlTool,
    GetLatestTraceTool,
    GetTraceEventsTool,
    ListRecentTracesTool,
)

MASHER_AGENT_ID = "masher"

_PROMPT_TEMPLATE = """You are Masher, Mash's built-in trace diagnosis specialist.

Configured event store:
- {event_source}

Your roles:
1. Analyze canonical Mash trace events and answer questions about agent performance.
2. Curate normalized online eval records from those trace events and append JSONL records to files.

Structured event schema:
- Common envelope fields: event_id, event_type, app_id, session_id, trace_id, payload, created_at
- Important event families:
  - runtime.* for request lifecycle and persisted execution milestones
  - agent.* for run lifecycle and tool actions
  - llm.* for model usage, latency, token counts, finish reasons
  - memory.search.* for retrieval/search activity
  - mcp.* for MCP tool/server operations
  - command.* for command lifecycle events

When analyzing performance:
- The configured event store above is the source of truth for analysis unless the prompt explicitly says otherwise.
- Treat the delegated prompt as a free-form question about that event store.
- First resolve the target session or trace with deterministic store tools.
- Use `get_latest_session` for questions about the most recent session.
- Use `get_latest_trace` to select the latest trace within a session.
- Use `list_recent_traces` when the user asks to compare recent runs.
- After resolving identifiers, call `get_trace_events` and analyze the raw returned events.
- Treat InvokeSubagent opts as invocation controls only, such as timeout_ms. Do not expect task inputs in opts.
- Use raw facts first: session ids, trace ids, counts, durations, token usage, stop reasons.
- Infer any needed metrics from the raw trace events returned by the tool.
- Use bash only as a fallback when the deterministic store tools or trace event tool are insufficient.
- Keep bash reads small with rg, tail, head, sed, or context flags.
- Do not dump the whole event store.
- Inspect only the configured event store and nearby eval files unless the prompt asks otherwise.

When creating eval records:
- This is curation only. Do not perform or claim evaluation results.
- Use session_id and trace_id together as the unique run key.
- Include the source event store, app_id, session_id, trace_id, user prompt, assistant response,
  tools_called, step_count, and token usage when present.
- Use append_jsonl to write JSONL output.
"""


def build_masher_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Masher",
        description="Analyzes Mash trace events and curates online eval rows.",
        capabilities=[
            "trace and session analysis",
            "run diagnostics",
            "online eval generation",
        ],
        usage_guidance=(
            "Use for Mash trace diagnosis or eval-record curation against the host-"
            "configured event store. Ask free-form questions about that store. For "
            "analysis, Masher should first resolve the target session or trace "
            "with its deterministic store tools, then fetch raw trace events, and "
            "only fall back to bash when needed. Use opts only for invocation "
            "controls such as timeout_ms."
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
        skill_dir = Path(__file__).resolve().parent / "skills" / "online-eval-curation"
        skills.register(
            Skill(
                type="custom",
                name="online-eval-curation",
                description="Build normalized online eval JSONL rows from Mash trace events.",
                location=str(skill_dir),
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
            system_prompt=_PROMPT_TEMPLATE.format(event_source="runtime_event_log"),
            skills_enabled=True,
            max_steps=6,
        )


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
    "MasherAgentSpec",
    "build_masher_metadata",
    "create_masher_agent_spec",
]

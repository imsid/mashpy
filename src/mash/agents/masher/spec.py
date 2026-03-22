"""Built-in Masher subagent spec."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider, OpenAIProvider
from mash.logging import EventLogger
from mash.memory.store import SQLiteStore
from mash.runtime.spec import AgentSpec
from mash.runtime.types import SubAgentMetadata
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry
from mash.tools.runtime import RuntimeToolBuilder

from .tool import AppendJsonlTool, GetTraceLogsTool

MASHER_AGENT_ID = "masher"

_PROMPT_TEMPLATE = """You are Masher, Mash's built-in log analysis specialist.

Configured log file:
- {log_file}

Your roles:
1. Analyze structured Mash event logs and answer questions about agent performance.
2. Curate normalized online eval records from those logs and append JSONL records to files.

Structured event schema:
- Common envelope fields: event_type, ts, app_id, session_id, event_class, payload
- Important event families:
  - agent.* for run lifecycle, steps, action type, tool calls, stop/completion
  - llm.* for model usage, latency, token counts, finish reasons
  - subagent.* for delegated work and subagent session tracking
  - memory.search.* for retrieval/search activity
  - mcp.* for MCP tool/server operations
  - command.* for command lifecycle events

When analyzing performance:
- The configured log file above is the source of truth for analysis unless the prompt explicitly says otherwise.
- Treat the delegated prompt as a free-form question about that log file.
- First resolve the target session or trace with deterministic store tools.
- Use `get_latest_session` for questions about the most recent session.
- Use `get_latest_trace` to select the latest trace within a session.
- Use `list_recent_traces` when the user asks to compare recent runs.
- After resolving identifiers, call `get_trace_logs` and analyze the raw returned events.
- Treat InvokeSubagent opts as invocation controls only, such as timeout_ms. Do not expect task inputs in opts.
- Use raw facts first: session ids, trace ids, counts, durations, token usage, stop reasons.
- Infer any needed metrics from the raw session records returned by the tool.
- Use bash only as a fallback when the deterministic store tools or raw log tool are insufficient.
- Keep bash reads small with rg, tail, head, sed, or context flags.
- Do not dump the whole log file.
- Inspect only the configured log file and nearby eval files unless the prompt asks otherwise.

When creating eval records:
- This is curation only. Do not perform or claim evaluation results.
- Use session_id and trace_id together as the unique run key.
- Include the source log path, app_id, session_id, trace_id, user prompt, assistant response,
  tools_called, step_count, and token usage when present.
- Use append_jsonl to write JSONL output.
"""


def build_masher_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Masher",
        description="Analyzes Mash event logs and curates online eval rows.",
        capabilities=[
            "log and session analysis",
            "run diagnostics",
            "online eval generation",
        ],
        usage_guidance=(
            "Use for Mash log analysis or eval-record curation against the host-"
            "configured log file. Ask free-form questions about that log. For "
            "analysis, Masher should first resolve the target session or trace "
            "with its deterministic store tools, then fetch raw log events, and "
            "only fall back to bash when needed. Use opts only for invocation "
            "controls such as timeout_ms."
        ),
    )


class MasherAgentSpec(AgentSpec):
    """Built-in log analysis subagent."""

    def __init__(
        self,
        log_file: Union[str, Path],
        *,
        target_app_id: str | None = None,
    ) -> None:
        self.log_file = Path(log_file).expanduser()
        self.target_app_id = (
            target_app_id.strip()
            if isinstance(target_app_id, str) and target_app_id.strip()
            else self.log_file.parent.parent.name
        )

    def _build_target_store(self) -> SQLiteStore:
        return SQLiteStore(self.log_file.parent.parent / "state.db")

    def get_agent_id(self) -> str:
        return MASHER_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        target_store = self._build_target_store()
        runtime_tools = RuntimeToolBuilder(
            store=target_store,
            app_id=self.target_app_id,
            event_logger=EventLogger(self.get_log_destination()),
            session_id=MASHER_AGENT_ID,
        )
        tools.register(runtime_tools.build_get_latest_session_tool())
        tools.register(runtime_tools.build_get_latest_trace_tool())
        tools.register(runtime_tools.build_list_recent_traces_tool())
        tools.register(GetTraceLogsTool(log_file=self.log_file))
        tools.register(BashTool(working_dir=str(self.log_file.parent)))
        tools.register(AppendJsonlTool())
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skill_dir = Path(__file__).resolve().parent / "skills" / "online-eval-curation"
        skills.register(
            Skill(
                type="custom",
                name="online-eval-curation",
                description="Build normalized online eval JSONL rows from Mash log files.",
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
            system_prompt=_PROMPT_TEMPLATE.format(log_file=str(self.log_file)),
            skills_enabled=True,
            max_steps=6,
        )


__all__ = ["MASHER_AGENT_ID", "MasherAgentSpec", "build_masher_metadata"]

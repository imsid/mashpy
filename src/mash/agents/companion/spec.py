"""The built-in companion agent, the primary of a host that names none."""

from __future__ import annotations

from mash.agents.masher.spec import _build_eval_llm
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.runtime.host.subagents import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .prompt import COMPANION_PROMPT

COMPANION_AGENT_ID = "companion"


def build_companion_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Companion",
        description=(
            "Built-in router: decides which agents and workflows handle each "
            "user message, waits for them and writes the answer."
        ),
        capabilities=["routing", "agent sessions", "workflow runs", "agent logs"],
        usage_guidance=(
            "The primary of every host defined without one. Apps do not route "
            "to it."
        ),
    )


class CompanionSpec(AgentSpec):
    """Routes with RunSubAgents and RunWorkflow, which the runtime registers."""

    def get_agent_id(self) -> str:
        return COMPANION_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        # The same provider selection as masher's built-in agents, with
        # COMPANION_AGENT_<PROVIDER>_MODEL overrides.
        return _build_eval_llm(
            agent_id=COMPANION_AGENT_ID, prefix="COMPANION_AGENT", label="companion agent"
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=COMPANION_AGENT_ID,
            system_prompt=COMPANION_PROMPT,
            # Ledger entries are short, so the companion keeps more turns.
            conversation_history_turns=10,
        )

    def enable_runtime_tools(self) -> bool:
        return False

    def enable_sandbox(self) -> bool:
        return False


__all__ = ["COMPANION_AGENT_ID", "CompanionSpec", "build_companion_metadata"]

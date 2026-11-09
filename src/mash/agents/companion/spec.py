"""The built-in companion agent, which routes every host request."""

from __future__ import annotations

from mash.agents.masher.spec import _build_eval_llm, _select_eval_agent_provider_kind
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.runtime.host.directory import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .logs import AgentLogReader, GetAgentTurnsTool, InspectAgentTraceTool, ListAgentTracesTool
from .prompt import COMPANION_PROMPT

COMPANION_AGENT_ID = "companion"


class _UnconfiguredLLM(LLMProvider):
    """The companion's model when no provider key is set.

    Every pool has the companion, so it must start without a key; a host
    request then fails with this error instead of the whole host.
    """

    @property
    def model(self) -> str:
        return "unconfigured"

    async def send(self, request):
        raise RuntimeError(
            "the companion has no model: set GEMINI_API_KEY, OPENAI_API_KEY, "
            "ANTHROPIC_API_KEY or OSS_BASE_URL on the host"
        )

    def set_event_logger(self, logger, app_id: str) -> None:
        del logger, app_id


def build_companion_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Companion",
        description=(
            "Built-in router: decides which agents and workflows handle each "
            "user message, waits for them and writes the answer."
        ),
        capabilities=["routing", "agent sessions", "workflow runs", "agent logs"],
        usage_guidance=(
            "Every request to a host goes to it. Hosts do not list it among "
            "their agents."
        ),
    )


class CompanionSpec(AgentSpec):
    """Routes with RunSubAgents and RunWorkflow, which the runtime registers,
    and reads its agents' logs with its own tools."""

    def __init__(self) -> None:
        self.logs = AgentLogReader(COMPANION_AGENT_ID)

    def get_agent_id(self) -> str:
        return COMPANION_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        for tool in (
            ListAgentTracesTool(self.logs),
            InspectAgentTraceTool(self.logs),
            GetAgentTurnsTool(self.logs),
        ):
            tools.register(tool)
        return tools

    def on_startup(self, runtime) -> None:
        # The process's own stores: the same Postgres every agent writes to.
        self.logs.bind(runtime.runtime_store, runtime.memory_store)

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        # The same provider selection as masher's built-in agents, with
        # COMPANION_AGENT_<PROVIDER>_MODEL overrides.
        if _select_eval_agent_provider_kind() is None:
            return _UnconfiguredLLM()
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

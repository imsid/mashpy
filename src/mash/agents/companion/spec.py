"""The built-in companion agent, which routes every host request."""

from __future__ import annotations

import os

from mash.agents.masher.spec import _build_eval_llm, _select_eval_agent_provider_kind
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.runtime.host.directory import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .classifier import CLASSIFIER_PROMPT

COMPANION_AGENT_ID = "companion"
BOARD_COMPACTION_TOKENS = 100_000


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

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


def build_companion_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Companion",
        description=(
            "Built-in router: picks the agent or workflow for each user "
            "message with a classifier and hands the message to it."
        ),
        capabilities=["routing", "workflow runs", "board summaries"],
        usage_guidance=(
            "Every request to a host goes to it. Hosts do not list it among "
            "their agents."
        ),
    )


def _routing_provider_options() -> dict:
    """Low thinking on Gemini: routing needs little, and each call is faster.

    ``COMPANION_AGENT_THINKING_LEVEL`` overrides the level; other providers
    get no options.
    """
    if _select_eval_agent_provider_kind() != "gemini":
        return {}
    level = os.getenv("COMPANION_AGENT_THINKING_LEVEL", "low").strip()
    return {"thinking_level": level} if level else {}


class CompanionSpec(AgentSpec):
    """Routes each host request with a classifier; it runs no agent loop.

    Its model is the classifier, and writes a conversation's board summary.
    """

    def get_agent_id(self) -> str:
        return COMPANION_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

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
            agent_id=COMPANION_AGENT_ID,
            system_prompt=CLASSIFIER_PROMPT,
            # The board's threshold: the companion summarizes a conversation's
            # board once its entries since the last summary reach this many
            # tokens. The board is in every model call of every request in the
            # conversation, so it sits well below the per-request default.
            compaction_token_threshold=BOARD_COMPACTION_TOKENS,
            provider_options=_routing_provider_options(),
        )

    def enable_runtime_tools(self) -> bool:
        return False

    def enable_sandbox(self) -> bool:
        return False


__all__ = ["COMPANION_AGENT_ID", "CompanionSpec", "build_companion_metadata"]

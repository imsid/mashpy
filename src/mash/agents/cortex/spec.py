"""The built-in cortex-agent, which curates an app's Cortex from its turns."""

from __future__ import annotations

from mash.agents.masher.spec import _build_eval_llm
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.runtime.host.directory import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .context import CortexRuntimeContext
from .models import CortexSettings

CORTEX_AGENT_ID = "cortex-agent"

_CURATOR_PROMPT = """You are Mash's built-in Cortex curator.

You are invoked only by the cortex-build workflow. Do not answer free-form chat.

The request is JSON with one app's current Cortex (a profile and entries) and
its new conversation turns with the pool's agents, from every session of the
app, including the tools those agents ran. Update the Cortex from the new turns:

- Keep facts the people in these conversations stated or confirmed:
  preferences, accounts and systems they use, ongoing work, standing
  instructions. Leave out one-off requests,
  small talk, and anything only an agent asserted.
- Each entry holds one fact under a short topic. Merge duplicates into one
  entry. Update an entry by returning it with its entry_id; add one with
  entry_id null.
- Delete build entries the new turns contradict or make obsolete by listing
  their entry_id in deletes.
- Entries with origin "app" were set by the application. Treat them as true.
  Never update or delete them.
- Give each entry you write the sources (agent_id, session_id, trace_id) of
  the turns it came from.
- Rewrite the profile as a short summary of what the app knows, under
  profile_max_chars characters, built from the entries.

Always answer with the structured output the request demands.
"""


def build_cortex_agent_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Cortex Agent",
        description="Built-in curator of per-app Cortex memory.",
        capabilities=["app memory curation"],
        usage_guidance=(
            "This agent powers the cortex-build workflow, which builds one app's "
            "Cortex from its conversations."
        ),
    )


class CortexAgentSpec(AgentSpec):
    """Tool-less curator; the workflow's code steps read and write Cortex."""

    def __init__(self, settings: CortexSettings) -> None:
        self.runtime_context = CortexRuntimeContext(settings=settings)

    def get_agent_id(self) -> str:
        return CORTEX_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        # The same provider selection as masher's built-in agents, with
        # CORTEX_AGENT_<PROVIDER>_MODEL overrides.
        return _build_eval_llm(
            agent_id=CORTEX_AGENT_ID, prefix="CORTEX_AGENT", label="cortex agent"
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            agent_id=CORTEX_AGENT_ID,
            system_prompt=_CURATOR_PROMPT,
            max_steps=4,
        )

    def enable_runtime_tools(self) -> bool:
        return False


__all__ = ["CORTEX_AGENT_ID", "CortexAgentSpec", "build_cortex_agent_metadata"]

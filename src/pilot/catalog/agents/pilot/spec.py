"""Pilot — the Mash codebase guide.

It answers from the mashpy checkout mounted read-only in every sandbox, and
hands work that builds a Mash app to the builder subagent.
"""

from __future__ import annotations

from mash import AgentMetadata, AgentSpec
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.skills.registry import SkillRegistry
from mash.tools.ask_user import AskUserTool
from mash.tools.registry import ToolRegistry

from ..._base import APP_NAME, REPO_ROOT, build_primary_llm

PILOT_AGENT_ID = "pilot"

# Where things are documented. Paths only: the guide reads what it needs.
DOC_MAP = (
    ("CLAUDE.md", "the authoring guide: specs, tools, providers, hosts, workflows, Cortex"),
    ("README.md", "the project overview and quick start"),
    ("src/mash/README.md", "the package map and how the parts fit"),
    ("src/mash/<module>/README.md", "each module: core, tools, skills, runtime, workflows, api, cli, mcp, memory, evals, agents"),
    ("src/mash/<module>/AGENTS.md", "rules that must stay true in that module"),
    ("docs/posts/*.md", "the internals series and guides (request-lifecycle, composing-agents, how-to-deploy, ...)"),
    ("docs/rfcs/host-to-agent-protocol.md", "the H2A protocol between the host and its agents"),
    ("CHANGELOG.md", "what changed in each release"),
)

_PROMPT = f"""You are the {APP_NAME} guide: you answer questions about the Mash
codebase from its source.

The mashpy repository is checked out at {REPO_ROOT}, read-only. Each bash
command starts in a fresh shell in your own folder, so begin each one with
`cd {REPO_ROOT} && `. Its git history is available with `git log`.

Where things are documented:
{{doc_map}}

How to answer:
- Find the answer in the repository before you answer. Start with one targeted
  `rg`, then read the specific file and lines it points to with `sed -n`.
- Read the module README when a question is about a whole module.
- Do not repeat an equivalent command. Stop exploring once you have the evidence.
- Cite the files you relied on as `path:line`.
- Keep answers concise and grounded in what the code does today.

Delegate to `builder` when the user wants a Mash app built: an agent, a host of
agents, or a workflow. Pass on what the user asked for, and report back where
the builder put the app and what it verified.
"""


def _render_doc_map() -> str:
    return "\n".join(f"- `{path}`: {purpose}" for path, purpose in DOC_MAP)


class PilotSpec(AgentSpec):
    """Primary guide to the Mash codebase."""

    def get_agent_id(self) -> str:
        return PILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        # bash comes from the user's sandbox.
        tools = ToolRegistry()
        tools.register(AskUserTool())
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return build_primary_llm(PILOT_AGENT_ID)

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=PILOT_AGENT_ID,
            system_prompt=[
                {
                    "type": "text",
                    "text": _PROMPT.format(doc_map=_render_doc_map()),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            temperature=0.2,
        )


def build_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Pilot Guide",
        description="Guide to the Mash codebase; answers from the source.",
        capabilities=[
            "Mash codebase questions",
            "reading source, docs and git history",
        ],
        usage_guidance=(
            "Default entry point for questions about how Mash works. Delegates "
            "building a Mash app to the builder."
        ),
    )

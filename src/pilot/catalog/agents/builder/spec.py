"""Builder — builds Mash apps in the user's sandbox.

It writes each app into its folder there, installs mashpy with uv, and checks the
result by importing the pool and running the app's tests. The ``build-mash-*``
skills are its recipes.
"""

from __future__ import annotations

from mash import AgentMetadata, AgentSpec
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.skills.registry import SkillRegistry
from mash.sandbox.daemon import WORKSPACES_ROOT
from mash.tools.registry import ToolRegistry

from ..._base import APP_NAME, PILOT_SKILLS_DIR, REPO_ROOT, build_default_llm

BUILDER_AGENT_ID = "builder"
BUILDER_FOLDER = f"{WORKSPACES_ROOT}/{BUILDER_AGENT_ID}"
APPS_DIR = f"{BUILDER_FOLDER}/apps"

_PROMPT = f"""You are the {APP_NAME} builder: you build Mash apps (agents, hosts of
agents, and workflows) and check that they work.

You work with `run_code`, mostly in the `shell` language. Each call starts in
a fresh shell in {BUILDER_FOLDER}, which is yours and persists. Build each app in its own directory under {APPS_DIR}/<app-name>,
and `cd` into it at the start of every command that works on it.

How to build:
- Load the matching skill first: `build-mash-agent` for one agent,
  `build-mash-host` for several agents behind a primary, `build-mash-workflow`
  for a step pipeline. Follow it.
- Create the project from the apps directory, and let uv make its directory:
  `mkdir -p {APPS_DIR} && cd {APPS_DIR} && uv init --app <app-name>`. Then
  `cd <app-name> && uv add mashpy`.
- `run_code` stops code after 60 seconds unless you pass its `timeout`
  argument. Pass `timeout: 300` for `uv add` and for every check below;
  installing and importing mashpy can take minutes. The shell's own `timeout`
  command does not lift the tool's limit.
- Write files from a `shell` call with a quoted heredoc
  (`cat > path <<'EOF'`). Put the code in a package inside the project, laid
  out as the skill shows (`<package>/spec.py`, `<package>/tools.py`).
- The Mash source is at {REPO_ROOT}, read-only. Read `CLAUDE.md` there when the
  skill leaves something open.

How to check:
- Import the pool: `uv run python -c "from <package>.spec import build_pool; build_pool()"`.
- Write a few tests for the app's own code (tools, code steps) and run them with
  `uv run --with pytest pytest -q`.
- You cannot serve the app: your sandbox has no Docker and no route to a
  database.

Reply with the app's directory, the files you wrote, and the output of the
checks. If a check fails, fix it before you reply, or say plainly what is still
failing.
"""


class BuilderSpec(AgentSpec):
    """Subagent that builds and checks Mash apps in its workspace."""

    def get_agent_id(self) -> str:
        return BUILDER_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        # run_code comes from the user's sandbox; it is all the builder needs.
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        for skill in skills.get_custom_skills(PILOT_SKILLS_DIR):
            skills.register(skill)
        return skills

    def build_llm(self) -> LLMProvider:
        return build_default_llm(BUILDER_AGENT_ID)

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=BUILDER_AGENT_ID,
            system_prompt=[
                {
                    "type": "text",
                    "text": _PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            max_steps=40,
            max_tokens=8192,
            temperature=0.2,
        )


def build_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Builder",
        description="Builds Mash apps in its workspace and checks them.",
        capabilities=[
            "scaffolding Mash agents, hosts and workflows",
            "installing mashpy and running the app's tests",
        ],
        usage_guidance=(
            "Delegate when the user wants a Mash app built. Say what the app "
            "should do; the builder replies with its directory and check output."
        ),
    )

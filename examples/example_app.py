"""Canonical Mash example app for local and container deployment."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from mash.api import MashHostConfig, run_host
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider
from mash.runtime import (
    AgentSpec,
    MashAgentHost,
    MashAgentHostBuilder,
    SubAgentMetadata,
)
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from ._bootstrap import load_example_env, require_anthropic_api_key

PRIMARY_AGENT_ID = "primary"
RESEARCH_AGENT_ID = "research"
DEFAULT_SUBAGENT_TIMEOUT_MS = 360_000

load_example_env()


class ResearchAgentSpec(AgentSpec):
    """Subagent that can inspect a local workspace with shell tools."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def get_agent_id(self) -> str:
        return RESEARCH_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skill_dir = Path(__file__).resolve().parent / "skills" / "repo-audit"
        skills.register(
            Skill(
                type="custom",
                name="repo-audit",
                description="Checklist for auditing a repository",
                location=str(skill_dir),
            )
        )
        return skills

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            app_id=RESEARCH_AGENT_ID,
            api_key=require_anthropic_api_key(),
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=RESEARCH_AGENT_ID,
            system_prompt=(
                "You are a repository research specialist. Gather evidence from the "
                "workspace, summarize findings concisely, and cite specific facts. "
                "If the prompt includes 'Working folder:', stay inside that folder "
                "when using shell tools."
            ),
            skills_enabled=True,
        )


class PrimaryAgentSpec(AgentSpec):
    """Primary agent that delegates repository analysis to the research subagent."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def get_agent_id(self) -> str:
        return PRIMARY_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            app_id=PRIMARY_AGENT_ID,
            api_key=require_anthropic_api_key(),
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=PRIMARY_AGENT_ID,
            system_prompt=(
                "You are the primary planning assistant.\n"
                "Use InvokeSubagent(agent_id, prompt, opts) when a task requires "
                "repository investigation.\n"
                f"- default opts.timeout_ms={DEFAULT_SUBAGENT_TIMEOUT_MS}\n"
                f"- include this line in the delegated prompt unless the user says otherwise: "
                f"'Working folder: {self.workspace_root}'"
            ),
            skills_enabled=False,
        )


def build_research_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Codebase Research Analyst",
        description="Inspects a local codebase and returns concise technical findings.",
        capabilities=[
            "repo code search",
            "implementation tracing",
            "technical summaries",
        ],
        usage_guidance="Use for repository investigation and source-backed answers.",
    )


def build_host(workspace_root: Path | None = None) -> MashAgentHost:
    """Build the canonical example host."""
    resolved_workspace_root = (workspace_root or Path(".")).resolve()
    return (
        MashAgentHostBuilder()
        .primary(PrimaryAgentSpec(resolved_workspace_root), agent_id=PRIMARY_AGENT_ID)
        .subagent(
            ResearchAgentSpec(resolved_workspace_root),
            agent_id=RESEARCH_AGENT_ID,
            metadata=build_research_metadata(),
        )
        .build()
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical Mash example app over the Mash host API."
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace folder exposed to the research subagent's BashTool.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API bind host.")
    parser.add_argument("--port", type=int, default=8000, help="API bind port.")
    parser.add_argument("--api-key", default=None, help="Optional API key.")
    args = parser.parse_args(argv)

    run_host(
        build_host(Path(args.workspace_root).resolve()),
        config=MashHostConfig(
            bind_host=args.host,
            bind_port=args.port,
            api_key=args.api_key,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

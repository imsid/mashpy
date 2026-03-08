"""Primary + host-managed subagent Mash CLI example."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from mash_cli import CLIAppShell, SubagentRegistration
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider
from mash.memory.store import MemoryStore, SQLiteStore
from mash.runtime import MashRuntimeDefinition, SubAgentMetadata
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from ._bootstrap import load_example_env, require_anthropic_api_key

PRIMARY_APP_ID = "pm-primary"
RESEARCH_APP_ID = "research-subagent"
DEFAULT_SUBAGENT_TIMEOUT_MS = 120_000
load_example_env()


class ResearchSubagentDefinition(MashRuntimeDefinition):
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_app_id(self) -> str:
        return RESEARCH_APP_ID

    def build_store(self) -> MemoryStore:
        return SQLiteStore(self.root / ".mash" / "research-subagent.db")

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.root)))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            app_id=RESEARCH_APP_ID,
            api_key=require_anthropic_api_key(),
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=RESEARCH_APP_ID,
            system_prompt=(
                "You are the research subagent. Focus on gathering evidence, "
                "summarizing findings, and citing concrete facts. "
                "If the prompt includes 'Working folder:', prioritize that path and "
                "avoid broad filesystem scans."
            ),
            skills_enabled=False,
        )

    def get_log_destination(self) -> Path:
        return self.root / ".mash" / "logs" / "research-subagent.jsonl"


class ProjectManagerDefinition(MashRuntimeDefinition):
    def __init__(self, root: Path, workspace_folder: Path) -> None:
        self.root = root
        self.workspace_folder = workspace_folder

    def get_app_id(self) -> str:
        return PRIMARY_APP_ID

    def build_store(self) -> MemoryStore:
        return SQLiteStore(self.root / ".mash" / "pm-primary.db")

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            app_id=PRIMARY_APP_ID,
            api_key=require_anthropic_api_key(),
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=PRIMARY_APP_ID,
            system_prompt=(
                "You are a primary planning assistant.\n"
                "When delegating to the research subagent via "
                "InvokeSubagent(agent_id, prompt, opts):\n"
                f"- set opts.timeout_ms={DEFAULT_SUBAGENT_TIMEOUT_MS} unless user asks otherwise\n"
                f"- include this line in prompt unless user asks otherwise: "
                f"'Working folder: {self.workspace_folder}'\n"
                "- if prompt includes a working folder, ask subagent to stay inside it "
                "when using shell/file tools."
            ),
            skills_enabled=False,
        )

    def get_log_destination(self) -> Path:
        return self.root / ".mash" / "logs" / "pm-primary.jsonl"


def build_research_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Codebase Research Analyst",
        description=(
            "Inspects local codebases, traces implementation details, and "
            "returns concise technical findings."
        ),
        capabilities=[
            "repo code search",
            "runtime flow tracing",
            "implementation summaries",
        ],
        usage_guidance=(
            "Use for repository investigation tasks such as locating symbols, "
            "explaining request flows, or validating behavior from source."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the primary/subagent Mash SDK host/client example."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Working directory for sqlite/log files (default: current dir).",
    )
    parser.add_argument(
        "--workspace-folder",
        default=".",
        help=(
            "Folder passed to the research subagent for local file analysis "
            f"(default timeout: {DEFAULT_SUBAGENT_TIMEOUT_MS}ms)."
        ),
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    workspace_folder = Path(args.workspace_folder).resolve()
    shell = CLIAppShell.from_definition(
        ProjectManagerDefinition(root, workspace_folder),
        subagents=[
            SubagentRegistration(
                definition=ResearchSubagentDefinition(root),
                agent_id="research",
                metadata=build_research_metadata(),
            )
        ],
    )
    try:
        shell.run()
        return 0
    finally:
        shell.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

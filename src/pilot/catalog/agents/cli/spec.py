"""CLI copilot agent spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.runtime import AgentMetadata
from mash.tools.registry import ToolRegistry

from ..._base import CopilotAgentSpec, build_bash_tool

CLI_COPILOT_AGENT_ID = "cli-copilot"
CLI_DOC_ROOTS = ("src/mash/cli",)


class CliCopilotSpec(CopilotAgentSpec):
    """Subagent specialized in the Mash CLI codepath."""

    def get_agent_id(self) -> str:
        return CLI_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return self._build_copilot_prompt_blocks(
            scope="src/mash/cli",
            doc_roots=CLI_DOC_ROOTS,
            cache_label="CLI",
            extra_rules=(
                "Prefer a single `rg` or one small `sed` read over repeated full-file dumps.",
                "Do not reread the same file with wider ranges unless the first read was genuinely insufficient.",
            ),
        )

    def build_agent_config(self) -> AgentConfig:
        return self._build_copilot_config(CLI_COPILOT_AGENT_ID)


def build_cli_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Mash CLI Copilot",
        description=(
            "Specialist for the Mash CLI surface, REPL flow, terminal rendering, "
            "command dispatch, client-side session behavior, and trace visualization."
        ),
        capabilities=[
            "src/mash/cli",
            "cli commands",
            "repl behavior",
            "terminal rendering",
            "command dispatch",
            "session routing",
            "/trace command",
            "trace rendering",
            "chain_renderer",
            "subagent trace rendering",
        ],
        usage_guidance=(
            "Use for questions centered on CLI entrypoints, command handling, REPL "
            "behavior, shell output, rendering, local versus remote CLI session flow, "
            "or the /trace command and how traces are rendered in the terminal."
        ),
    )


def create_cli_copilot_spec(*, workspace_root: str) -> CliCopilotSpec:
    return CliCopilotSpec(Path(workspace_root).resolve())

"""MCP copilot agent spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.runtime import AgentMetadata
from mash.tools.registry import ToolRegistry

from ..._base import CopilotAgentSpec, build_bash_tool

MCP_COPILOT_AGENT_ID = "mcp-copilot"
MCP_DOC_ROOTS = ("src/mash/mcp",)


class McpCopilotSpec(CopilotAgentSpec):
    """Subagent specialized in Mash MCP integration."""

    def get_agent_id(self) -> str:
        return MCP_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return self._build_copilot_prompt_blocks(
            scope="src/mash/mcp",
            doc_roots=MCP_DOC_ROOTS,
            cache_label="MCP",
        )

    def build_agent_config(self) -> AgentConfig:
        return self._build_copilot_config(MCP_COPILOT_AGENT_ID)


def build_mcp_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Mash MCP Copilot",
        description=(
            "Specialist for Mash MCP transport, server and client wiring, manager "
            "configuration, and MCP integration behavior."
        ),
        capabilities=[
            "src/mash/mcp",
            "mcp client and manager",
            "mcp server configuration",
            "mcp transport",
            "tool adaptation",
            "host integration",
        ],
        usage_guidance=(
            "Use for questions centered on MCP managers, client/server behavior, "
            "MCP configuration, transport details, or Mash integration with MCP-backed tools."
        ),
    )


def create_mcp_copilot_spec(*, workspace_root: str) -> McpCopilotSpec:
    return McpCopilotSpec(Path(workspace_root).resolve())

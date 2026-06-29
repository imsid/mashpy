"""Pilot — the primary Mash codebase guide plus five module copilots (`cli`,
`api`, `mcp`, `runtime`, `workflow`). Their composition into the `guide`
host ships as the default entry in the CLI's host config file
(`pilot.store`), not in code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.mcp.types import MCPServerConfig
from mash.runtime import AgentMetadata, AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.ask_user import AskUserTool
from mash.tools.registry import ToolRegistry

from ....prompt import build_base_prompt, build_repo_context
from ....tools import UpdateDocsTool
from ..._base import (
    APP_NAME,
    PILOT_SKILLS_DIR,
    build_bash_tool,
    build_default_llm,
    scope_doc_paths,
)
from ..admin import ADMIN_COPILOT_AGENT_ID
from ..api import API_COPILOT_AGENT_ID
from ..cli import CLI_COPILOT_AGENT_ID
from ..mcp import MCP_COPILOT_AGENT_ID
from ..runtime import RUNTIME_COPILOT_AGENT_ID
from ..workflow import WORKFLOW_COPILOT_AGENT_ID

PILOT_AGENT_ID = "pilot"
DEFAULT_SUBAGENT_TIMEOUT_MS = 360_000

PILOT_DOC_ROOTS = (
    "src/mash/core",
    "src/mash/tools",
    "src/mash/skills",
    "src/mash/logging",
    "src/mash/memory",
    "src/mash/agents/masher",
)
PILOT_EXTRA_DOC_PATHS = (
    "README.md",
    "src/mash/README.md",
    "docs/posts/product-brief.md",
    "docs/posts/building-agent-clis.md",
    "docs/posts/building-dynamic-hosts-apis.md",
    "docs/posts/how-to-deploy.md",
    "docs/rfcs/host-to-agent-protocol.md",
)

GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL") or "https://api.githubcopilot.com/mcp/"
GITHUB_MCP_PAT = os.getenv("GITHUB_MCP_PAT")
GITHUB_MCP_CONNECTION_NAME = "github"


class PilotSpec(AgentSpec):
    """Primary guide specialized in the Mash codebase."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return PILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        tools.register(UpdateDocsTool(workspace_root=str(self.workspace_root)))
        tools.register(AskUserTool())
        return tools

    def build_llm(self) -> LLMProvider:
        return build_default_llm(self.get_agent_id())

    def build_mcp_servers(self) -> list[MCPServerConfig]:
        github_mcp_url = os.getenv("GITHUB_MCP_URL") or GITHUB_MCP_URL
        github_mcp_pat = os.getenv("GITHUB_MCP_PAT") or GITHUB_MCP_PAT
        if not github_mcp_url or not github_mcp_pat:
            return []
        return [
            MCPServerConfig(
                name=GITHUB_MCP_CONNECTION_NAME,
                url=github_mcp_url,
                description="GitHub MCP server for mashpy repository inspection",
                headers={"Authorization": f"Bearer {github_mcp_pat}"},
                allowed_tools=[
                    "list_commits",
                    "get_commit",
                ],
            )
        ]

    def build_skills(self) -> SkillRegistry:
        registry = SkillRegistry()
        for skill in registry.get_custom_skills(PILOT_SKILLS_DIR):
            registry.register(skill)
        return registry

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "\n".join(
                    [
                        build_base_prompt(
                            repo=str(self.workspace_root),
                            role=f"You are the primary Mash codebase guide in {APP_NAME}.",
                            extra_rules=(
                                "Handle shared and core questions for `src/mash/core`, `src/mash/tools`, `src/mash/skills`, `src/mash/logging`, `src/mash/memory`, and other cross-cutting codebase behavior.",
                                f"Delegate to `{CLI_COPILOT_AGENT_ID}`, `{API_COPILOT_AGENT_ID}`, `{MCP_COPILOT_AGENT_ID}`, `{RUNTIME_COPILOT_AGENT_ID}`, `{WORKFLOW_COPILOT_AGENT_ID}`, or `{ADMIN_COPILOT_AGENT_ID}` when the question is centered on that module.",
                                "Return one synthesized answer after any delegation.",
                                "If a subagent call fails or returns an incomplete answer, do not repeat the same delegation blindly; use your own cached docs and one targeted bash lookup to finish the answer when possible.",
                                "For observability, telemetry, trace analysis, or span questions: delegate data model and analysis questions (spans, TraceAnalysis, timing breakdowns, tool stats) to `runtime-copilot`, API endpoint questions (/telemetry/traces, /telemetry/trace/analysis, event streaming) to `api-copilot`, CLI rendering questions (/trace command, chain_renderer, subagent trace rendering) to `cli-copilot`. For cross-cutting observability questions, prefer `runtime-copilot` as the primary delegate.",
                                "For the admin dashboard UI — whether something is tracked or visible in it, what a tab or field means, which tab shows a thing, or which endpoint feeds a tab — delegate to `admin-copilot`; keep route internals with `api-copilot` and telemetry data-model questions with `runtime-copilot`.",
                                "If you need direct code verification, use one targeted bash command and answer directly.",
                                f"Default delegated opts.timeout_ms={DEFAULT_SUBAGENT_TIMEOUT_MS}.",
                                "Include the working folder in delegated prompts unless the user says otherwise:",
                                f"'Working folder: {self.workspace_root}'",
                            ),
                        ),
                    ]
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=scope_doc_paths(
                self.workspace_root,
                doc_roots=PILOT_DOC_ROOTS,
                extra_doc_paths=PILOT_EXTRA_DOC_PATHS,
            ),
        )
        if repo_context:
            blocks.append(
                {
                    "type": "text",
                    "text": repo_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=PILOT_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=True,
            temperature=0.2,
        )


def create_spec(*, workspace_root: str) -> PilotSpec:
    return PilotSpec(Path(workspace_root).resolve())


def build_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Pilot Guide",
        description=(
            "Primary Mash codebase guide; handles core, tools, skills, logging, "
            "memory, and cross-cutting questions."
        ),
        capabilities=[
            "src/mash/core",
            "src/mash/tools",
            "src/mash/skills",
            "src/mash/logging",
            "src/mash/memory",
            "cross-cutting codebase questions",
            "answer synthesis across modules",
        ],
        usage_guidance=(
            "Default entry point for Mash codebase questions. Use for shared and "
            "core behavior, or questions that span multiple modules; module-"
            "centered questions belong to the matching copilot."
        ),
    )

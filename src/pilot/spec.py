"""Pilot pool: all agent specs and pool assembly in one place.

Defining the spec classes here (rather than only in the catalog sub-packages)
makes them patchable via ``pilot.spec.<name>`` in tests, and keeps the pool
assembly self-contained with a single import surface.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Sequence

from mash.api import MashHostConfig, run_host
from mash.core.config import AgentConfig
from mash.mcp.types import MCPServerConfig
from mash.runtime import AgentMetadata, AgentPool, AgentSpec, HostBuilder
from mash.runtime.host.types import Host
from mash.skills.registry import SkillRegistry
from mash.tools.ask_user import AskUserTool
from mash.tools.registry import ToolRegistry

from .catalog._base import (
    APP_NAME,
    COMMON_COPILOT_RULES,
    COMMON_SEARCH_RULES,
    PILOT_SKILLS_DIR,
    build_bash_tool,
    build_default_llm,
    scope_doc_paths,
)
from .catalog.agents.api.spec import build_api_metadata
from .catalog.agents.cli.spec import build_cli_metadata
from .catalog.agents.mcp.spec import build_mcp_metadata
from .catalog.agents.pilot.spec import (
    DEFAULT_SUBAGENT_TIMEOUT_MS,
    GITHUB_MCP_CONNECTION_NAME,
    GITHUB_MCP_PAT,
    GITHUB_MCP_URL,
    PILOT_DOC_ROOTS,
    build_metadata as _build_pilot_metadata,
)

PILOT_EXTRA_DOC_PATHS = (
    "README.md",
    "src/mash/README.md",
    "src/mash/AGENTS.md",
    "docs/posts/product-brief.md",
    "docs/posts/building-agent-clis.md",
    "docs/posts/building-dynamic-hosts-apis.md",
    "docs/posts/how-to-deploy.md",
    "docs/rfcs/host-to-agent-protocol.md",
)
from .catalog.agents.runtime.spec import build_runtime_metadata
from .catalog.agents.workflow.spec import build_workflow_metadata
from .prompt import build_base_prompt, build_repo_context
from .tools import UpdateDocsTool

# ── Agent ID constants ──────────────────────────────────────────────────────

PILOT_AGENT_ID = "pilot"
CLI_COPILOT_AGENT_ID = "cli-copilot"
API_COPILOT_AGENT_ID = "api-copilot"
MCP_COPILOT_AGENT_ID = "mcp-copilot"
RUNTIME_COPILOT_AGENT_ID = "runtime-copilot"
WORKFLOW_COPILOT_AGENT_ID = "workflow-copilot"


# ── Cached-docs helper (test-patchable) ─────────────────────────────────────

def _cached_docs_for_scope(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str] = (),
    extra_doc_paths: Sequence[str] = (),
) -> list[str]:
    """Collect cached doc paths for a scope.

    This is the single indirection point that tests patch to inject fake
    doc paths without touching the filesystem.
    """
    return scope_doc_paths(
        workspace_root,
        doc_roots=doc_roots,
        extra_doc_paths=extra_doc_paths,
    )


# ── Shared prompt helpers ────────────────────────────────────────────────────

def _copilot_prompt_blocks(
    workspace_root: Path,
    *,
    scope: str,
    doc_roots: Sequence[str],
    cache_label: str,
    extra_rules: Sequence[str] = (),
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_base_prompt(
                repo=str(workspace_root),
                role=f"You are the {APP_NAME} copilot for `{scope}`.",
                extra_rules=(
                    *COMMON_COPILOT_RULES,
                    f"Use the cached {cache_label} docs before using bash.",
                    *COMMON_SEARCH_RULES,
                    *extra_rules,
                ),
            ),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    repo_context = build_repo_context(
        repo=str(workspace_root),
        cached_files=_cached_docs_for_scope(workspace_root, doc_roots=doc_roots),
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


def _copilot_config(agent_id: str, *, system_prompt: list[dict]) -> AgentConfig:
    return AgentConfig(
        app_id=agent_id,
        system_prompt=system_prompt,
        skills_enabled=False,
        conversation_history_turns=0,
        max_steps=10,
        temperature=0.2,
    )


# ── Agent spec classes ───────────────────────────────────────────────────────

class PilotSpec(AgentSpec):
    """Primary Mash codebase guide."""

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

    def build_skills(self) -> SkillRegistry:
        registry = SkillRegistry()
        for skill in registry.get_custom_skills(PILOT_SKILLS_DIR):
            registry.register(skill)
        return registry

    def build_llm(self):
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
                allowed_tools=["list_commits", "get_commit"],
            )
        ]

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
                                f"Delegate to `{CLI_COPILOT_AGENT_ID}`, `{API_COPILOT_AGENT_ID}`, `{MCP_COPILOT_AGENT_ID}`, `{RUNTIME_COPILOT_AGENT_ID}`, or `{WORKFLOW_COPILOT_AGENT_ID}` when the question is centered on that module.",
                                "Return one synthesized answer after any delegation.",
                                "If a subagent call fails or returns an incomplete answer, do not repeat the same delegation blindly; use your own cached docs and one targeted bash lookup to finish the answer when possible.",
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
        all_cached = _cached_docs_for_scope(
            self.workspace_root,
            doc_roots=PILOT_DOC_ROOTS,
            extra_doc_paths=PILOT_EXTRA_DOC_PATHS,
        )
        # RFC docs are fetched via extra_doc_paths to confirm existence and prime
        # the Anthropic cache, but they are embedded via the skills system rather
        # than rendered as DOC entries in the cached-docs block.
        rendered_cached = [d for d in all_cached if "/rfcs/" not in d]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=rendered_cached,
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


class CliCopilotSpec(AgentSpec):
    """Subagent specialized in the Mash CLI codepath."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return CLI_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return _copilot_prompt_blocks(
            self.workspace_root,
            scope="src/mash/cli",
            doc_roots=("src/mash/cli",),
            cache_label="CLI",
            extra_rules=(
                "Prefer a single `rg` or one small `sed` read over repeated full-file dumps.",
                "Do not reread the same file with wider ranges unless the first read was genuinely insufficient.",
            ),
        )

    def build_agent_config(self) -> AgentConfig:
        return _copilot_config(
            CLI_COPILOT_AGENT_ID, system_prompt=self.build_system_prompt()
        )


class ApiCopilotSpec(AgentSpec):
    """Subagent specialized in the Mash API codepath."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return API_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return _copilot_prompt_blocks(
            self.workspace_root,
            scope="src/mash/api",
            doc_roots=("src/mash/api",),
            cache_label="API",
        )

    def build_agent_config(self) -> AgentConfig:
        return _copilot_config(
            API_COPILOT_AGENT_ID, system_prompt=self.build_system_prompt()
        )


class McpCopilotSpec(AgentSpec):
    """Subagent specialized in Mash MCP integration."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return MCP_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return _copilot_prompt_blocks(
            self.workspace_root,
            scope="src/mash/mcp",
            doc_roots=("src/mash/mcp",),
            cache_label="MCP",
        )

    def build_agent_config(self) -> AgentConfig:
        return _copilot_config(
            MCP_COPILOT_AGENT_ID, system_prompt=self.build_system_prompt()
        )


class RuntimeCopilotSpec(AgentSpec):
    """Subagent specialized in Mash runtime hosting and durability."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return RUNTIME_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return _copilot_prompt_blocks(
            self.workspace_root,
            scope="src/mash/runtime",
            doc_roots=("src/mash/runtime",),
            cache_label="runtime",
        )

    def build_agent_config(self) -> AgentConfig:
        return _copilot_config(
            RUNTIME_COPILOT_AGENT_ID, system_prompt=self.build_system_prompt()
        )


class WorkflowCopilotSpec(AgentSpec):
    """Subagent specialized in Mash workflow orchestration."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return WORKFLOW_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    def build_system_prompt(self) -> list[dict[str, Any]]:
        return _copilot_prompt_blocks(
            self.workspace_root,
            scope="src/mash/workflows",
            doc_roots=("src/mash/workflows",),
            cache_label="workflow",
        )

    def build_agent_config(self) -> AgentConfig:
        return _copilot_config(
            WORKFLOW_COPILOT_AGENT_ID, system_prompt=self.build_system_prompt()
        )


__all__ = [
    "APP_NAME",
    "API_COPILOT_AGENT_ID",
    "ApiCopilotSpec",
    "CLI_COPILOT_AGENT_ID",
    "CliCopilotSpec",
    "MCP_COPILOT_AGENT_ID",
    "McpCopilotSpec",
    "PILOT_AGENT_ID",
    "PilotSpec",
    "RUNTIME_COPILOT_AGENT_ID",
    "RuntimeCopilotSpec",
    "WORKFLOW_COPILOT_AGENT_ID",
    "WorkflowCopilotSpec",
    "_cached_docs_for_scope",
]


# ── Pool assembly ────────────────────────────────────────────────────────────

def build_pool(workspace_root: Path | None = None) -> AgentPool:
    """Build the Pilot agent pool.

    Registers the six pilot agents and defines a default ``guide`` host that
    composes them. The pool ships no other host compositions; dynamic hosts are
    defined over the API at runtime.
    """
    _repo_root = Path(__file__).resolve().parents[2]
    resolved_workspace_root = (
        workspace_root or Path(os.environ.get("PILOT_WORKSPACE_ROOT", str(_repo_root)))
    ).resolve()
    ws = resolved_workspace_root

    builder = HostBuilder()
    builder.agent(PilotSpec(ws), metadata=_build_pilot_metadata())
    builder.agent(CliCopilotSpec(ws), metadata=build_cli_metadata())
    builder.agent(ApiCopilotSpec(ws), metadata=build_api_metadata())
    builder.agent(McpCopilotSpec(ws), metadata=build_mcp_metadata())
    builder.agent(RuntimeCopilotSpec(ws), metadata=build_runtime_metadata())
    builder.agent(WorkflowCopilotSpec(ws), metadata=build_workflow_metadata())
    builder.host(
        Host(
            host_id="guide",
            primary=PILOT_AGENT_ID,
            subagents=(
                API_COPILOT_AGENT_ID,
                CLI_COPILOT_AGENT_ID,
                MCP_COPILOT_AGENT_ID,
                RUNTIME_COPILOT_AGENT_ID,
                WORKFLOW_COPILOT_AGENT_ID,
            ),
        )
    )
    builder.enable_masher()
    return builder.build()


# Back-compat alias: existing deployments may still point MASH_HOST_APP at
# `pilot.spec:build_host`. Drop once they are confirmed on `build_pool`.
build_host = build_pool


def serve(
    *,
    workspace_root: str = ".",
    bind_host: str = "127.0.0.1",
    bind_port: int = 8000,
    api_key: str | None = None,
) -> int:
    """Run the Pilot host API over the pool. Blocks until shutdown."""
    run_host(
        build_pool(Path(workspace_root).resolve()),
        config=MashHostConfig(
            bind_host=bind_host,
            bind_port=bind_port,
            api_key=api_key,
        ),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Mash Pilot host over the Mash host API."
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace folder exposed to the Mash pilot subagents.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API bind host.")
    parser.add_argument("--port", type=int, default=8000, help="API bind port.")
    parser.add_argument("--api-key", default=None, help="Optional API key.")
    args = parser.parse_args(argv)

    return serve(
        workspace_root=args.workspace_root,
        bind_host=args.host,
        bind_port=args.port,
        api_key=args.api_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())

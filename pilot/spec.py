"""Pilot agent for mashpy codebase along with its copilots."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from mash.api import MashHostConfig, run_host
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.core.llm.anthropic import AnthropicProvider
from mash.runtime import (
    AgentSpec,
    AgentHost,
    HostBuilder,
    SubAgentMetadata,
)
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from .prompt import build_base_prompt, build_repo_context

APP_NAME = "Mash Pilot"
PILOT_AGENT_ID = "pilot"
CLI_COPILOT_AGENT_ID = "cli-copilot"
API_COPILOT_AGENT_ID = "api-copilot"
MCP_COPILOT_AGENT_ID = "mcp-copilot"
RUNTIME_COPILOT_AGENT_ID = "runtime-copilot"
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
    "src/mash/AGENTS.md",
    "docs/rfcs/host-to-agent-protocol.md",
)

CLI_DOC_ROOTS = ("src/mash/cli",)
API_DOC_ROOTS = ("src/mash/api",)
MCP_DOC_ROOTS = ("src/mash/mcp",)
RUNTIME_DOC_ROOTS = ("src/mash/runtime",)


def _load_pilot_env() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")


def _scope_doc_paths(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str],
    extra_doc_paths: Sequence[str] = (),
) -> list[str]:
    doc_paths: list[str] = []
    seen: set[str] = set()

    for root in doc_roots:
        root_path = (workspace_root / root).resolve()
        for filename in ("README.md", "AGENTS.md"):
            candidate = root_path / filename
            if candidate.is_file():
                resolved = str(candidate)
                if resolved not in seen:
                    seen.add(resolved)
                    doc_paths.append(resolved)

    for relpath in extra_doc_paths:
        candidate = (workspace_root / relpath).resolve()
        if candidate.is_file():
            resolved = str(candidate)
            if resolved not in seen:
                seen.add(resolved)
                doc_paths.append(resolved)

    return doc_paths


def _cached_docs_for_scope(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str] = (),
    extra_doc_paths: Sequence[str] = (),
) -> list[str]:
    return _scope_doc_paths(
        workspace_root,
        doc_roots=doc_roots,
        extra_doc_paths=extra_doc_paths,
    )


_load_pilot_env()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


class _BaseCopilotSpec(AgentSpec):
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        """
        return OpenAIProvider(
            app_id=self.get_agent_id(),
            model=OPENAI_MODEL,
            api_key=OPENAI_API_KEY,
        )
        """
        return AnthropicProvider(
            app_id=self.get_agent_id(),
            model=ANTHROPIC_MODEL,
            api_key=ANTHROPIC_API_KEY,
        )

    def enable_runtime_tools(self) -> bool:
        return True


class CliCopilotSpec(_BaseCopilotSpec):
    """Subagent specialized in the Mash CLI codepath."""

    def get_agent_id(self) -> str:
        return CLI_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_base_prompt(
                    repo=str(self.workspace_root),
                    role=f"You are the {APP_NAME} copilot for `src/mash/cli`.",
                    extra_rules=(
                        "If the delegated prompt asks you to perform a focused codebase task, do it and answer directly instead of asking back-and-forth permission questions.",
                        "Use the cached CLI docs before using bash.",
                        "Use bash only when one targeted verification is still needed.",
                        "For command, inventory, or existence questions, start with one targeted `rg` and answer as soon as it gives enough evidence.",
                        "Use `sed` only after `rg` points to a specific file and line range that needs verification.",
                        "Prefer a single `rg` or one small `sed` read over repeated full-file dumps.",
                        "Do not reread the same file with wider ranges unless the first read was genuinely insufficient.",
                        "Do not repeat an equivalent bash command.",
                        "Do not ask the user for permission to inspect code; inspect the code and answer directly.",
                        "If you already have enough evidence, stop and answer instead of continuing to explore.",
                    ),
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=_cached_docs_for_scope(
                self.workspace_root,
                doc_roots=CLI_DOC_ROOTS,
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
            app_id=CLI_COPILOT_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=10,
            temperature=0.2,
        )


class ApiCopilotSpec(_BaseCopilotSpec):
    """Subagent specialized in the Mash API codepath."""

    def get_agent_id(self) -> str:
        return API_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_base_prompt(
                    repo=str(self.workspace_root),
                    role=f"You are the {APP_NAME} copilot for `src/mash/api`.",
                    extra_rules=(
                        "If the delegated prompt asks you to perform a focused codebase task, do it and answer directly instead of asking back-and-forth permission questions.",
                        "Use the cached API docs before using bash.",
                        "Use bash only when one targeted verification is still needed.",
                        "For command, inventory, or existence questions, start with one targeted `rg` and answer as soon as it gives enough evidence.",
                        "Use `sed` only after `rg` points to a specific file and line range that needs verification.",
                        "Prefer a single `rg` or one small `sed` read over repeated broad reads.",
                        "Do not repeat an equivalent bash command.",
                        "Do not ask the user for permission to inspect code; inspect the code and answer directly.",
                        "If you already have enough evidence, stop and answer instead of continuing to explore.",
                    ),
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=_cached_docs_for_scope(
                self.workspace_root,
                doc_roots=API_DOC_ROOTS,
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
            app_id=API_COPILOT_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=10,
            temperature=0.2,
        )


class McpCopilotSpec(_BaseCopilotSpec):
    """Subagent specialized in Mash MCP integration."""

    def get_agent_id(self) -> str:
        return MCP_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_base_prompt(
                    repo=str(self.workspace_root),
                    role=f"You are the {APP_NAME} copilot for `src/mash/mcp`.",
                    extra_rules=(
                        "If the delegated prompt asks you to perform a focused codebase task, do it and answer directly instead of asking back-and-forth permission questions.",
                        "Use the cached MCP docs before using bash.",
                        "Use bash only when one targeted verification is still needed.",
                        "For command, inventory, or existence questions, start with one targeted `rg` and answer as soon as it gives enough evidence.",
                        "Use `sed` only after `rg` points to a specific file and line range that needs verification.",
                        "Prefer a single `rg` or one small `sed` read over repeated broad reads.",
                        "Do not repeat an equivalent bash command.",
                        "Do not ask the user for permission to inspect code; inspect the code and answer directly.",
                        "If you already have enough evidence, stop and answer instead of continuing to explore.",
                    ),
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=_cached_docs_for_scope(
                self.workspace_root,
                doc_roots=MCP_DOC_ROOTS,
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
            app_id=MCP_COPILOT_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=10,
            temperature=0.2,
        )


class RuntimeCopilotSpec(_BaseCopilotSpec):
    """Subagent specialized in Mash runtime hosting and durability."""

    def get_agent_id(self) -> str:
        return RUNTIME_COPILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_base_prompt(
                    repo=str(self.workspace_root),
                    role=f"You are the {APP_NAME} copilot for `src/mash/runtime`.",
                    extra_rules=(
                        "If the delegated prompt asks you to perform a focused codebase task, do it and answer directly instead of asking back-and-forth permission questions.",
                        "Use the cached runtime docs before using bash.",
                        "Use bash only when one targeted verification is still needed.",
                        "For command, inventory, or existence questions, start with one targeted `rg` and answer as soon as it gives enough evidence.",
                        "Use `sed` only after `rg` points to a specific file and line range that needs verification.",
                        "Prefer a single `rg` or one small `sed` read over repeated broad reads.",
                        "Do not repeat an equivalent bash command.",
                        "Do not ask the user for permission to inspect code; inspect the code and answer directly.",
                        "If you already have enough evidence, stop and answer instead of continuing to explore.",
                    ),
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=_cached_docs_for_scope(
                self.workspace_root,
                doc_roots=RUNTIME_DOC_ROOTS,
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
            app_id=RUNTIME_COPILOT_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=10,
            temperature=0.2,
        )


class PilotSpec(_BaseCopilotSpec):
    """Primary pilot specialized in Mash codebase."""

    def get_agent_id(self) -> str:
        return PILOT_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.workspace_root)))
        return tools

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
                                f"Delegate to `{CLI_COPILOT_AGENT_ID}`, `{API_COPILOT_AGENT_ID}`, `{MCP_COPILOT_AGENT_ID}`, or `{RUNTIME_COPILOT_AGENT_ID}` when the question is centered on that module.",
                                "Return one synthesized answer after any delegation.",
                                "If a subagent call fails or returns an incomplete answer, do not repeat the same delegation blindly; use your own cached docs and one targeted bash lookup to finish the answer when possible.",
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
            cached_files=_cached_docs_for_scope(
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
            skills_enabled=False,
            temperature=0.2,
        )


def build_cli_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Mash CLI Copilot",
        description=(
            "Specialist for the Mash CLI surface, REPL flow, terminal rendering, "
            "command dispatch, and client-side session behavior."
        ),
        capabilities=[
            "src/mash/cli",
            "cli commands",
            "repl behavior",
            "terminal rendering",
            "command dispatch",
            "session routing",
        ],
        usage_guidance=(
            "Use for questions centered on CLI entrypoints, command handling, REPL "
            "behavior, shell output, rendering, or local versus remote CLI session flow."
        ),
    )


def build_api_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Mash API Copilot",
        description=(
            "Specialist for the Mash hosted API surface, FastAPI app wiring, host "
            "serving entrypoints, and telemetry UI integration."
        ),
        capabilities=[
            "src/mash/api",
            "host api",
            "telemetry ui",
            "fastapi app wiring",
            "host serving",
            "api configuration",
        ],
        usage_guidance=(
            "Use for questions centered on the API app, host startup, HTTP-facing "
            "configuration, telemetry UI assets, or other behavior implemented under `src/mash/api`."
        ),
    )


def build_mcp_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
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


def build_runtime_metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Mash Runtime Copilot",
        description=(
            "Specialist for Mash runtime hosting, request handling, event sourcing, "
            "durable workflow execution, and subagent/runtime integration."
        ),
        capabilities=[
            "src/mash/runtime",
            "agent runtime",
            "host composition",
            "request handling",
            "event sourcing",
            "durable workflow execution",
            "subagent runtime integration",
        ],
        usage_guidance=(
            "Use for questions centered on AgentRuntime behavior, runtime host "
            "composition, request lifecycle, event replay, workflow durability, "
            "or other behavior implemented under `src/mash/runtime`."
        ),
    )


def create_pilot_spec(*, workspace_root: str) -> PilotSpec:
    return PilotSpec(Path(workspace_root).resolve())


def create_cli_copilot_spec(*, workspace_root: str) -> CliCopilotSpec:
    return CliCopilotSpec(Path(workspace_root).resolve())


def create_api_copilot_spec(*, workspace_root: str) -> ApiCopilotSpec:
    return ApiCopilotSpec(Path(workspace_root).resolve())


def create_mcp_copilot_spec(*, workspace_root: str) -> McpCopilotSpec:
    return McpCopilotSpec(Path(workspace_root).resolve())


def create_runtime_copilot_spec(*, workspace_root: str) -> RuntimeCopilotSpec:
    return RuntimeCopilotSpec(Path(workspace_root).resolve())


def build_host(workspace_root: Path | None = None) -> AgentHost:
    """Build the Mash Pilot Agent host."""
    resolved_workspace_root = (workspace_root or Path(".")).resolve()
    return (
        HostBuilder()
        .primary(create_pilot_spec(workspace_root=str(resolved_workspace_root)))
        .subagent(
            create_cli_copilot_spec(workspace_root=str(resolved_workspace_root)),
            metadata=build_cli_metadata(),
        )
        .subagent(
            create_api_copilot_spec(workspace_root=str(resolved_workspace_root)),
            metadata=build_api_metadata(),
        )
        .subagent(
            create_mcp_copilot_spec(workspace_root=str(resolved_workspace_root)),
            metadata=build_mcp_metadata(),
        )
        .subagent(
            create_runtime_copilot_spec(workspace_root=str(resolved_workspace_root)),
            metadata=build_runtime_metadata(),
        )
        .enable_masher()
        .build()
    )


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

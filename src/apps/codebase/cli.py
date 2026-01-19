"""Codebase Q&A agent CLI for MashPy."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from mash import AgentConfig, Mash
from mash.commands import Command, CommandBus
from mash.context import CLIContext
from mashnet import MCPClientError

from .config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    GITHUB_MCP_PAT,
    GITHUB_MCP_URL,
)

GITHUB_CONNECTION_NAME = "GitHub"


class CodebaseAgent(Mash):
    """MashPy agent for answering questions about codebases."""

    def __init__(self, **kwargs: Any) -> None:
        self.current_repo_path: Optional[str] = None
        self.current_repo_type = "none"
        kwargs.setdefault(
            "log_path", Path(__file__).resolve().with_name("codebase.log")
        )
        agent_config = AgentConfig(
            app_id="codebase-agent",
            system_prompt=self._build_system_prompt(),
            app_context=self._build_app_context(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            max_history_messages=20,
            tool_search_enabled=True,
            anthropic_api_key=ANTHROPIC_API_KEY,
            use_bash_tool=False,
            bash_working_dir=None,
        )
        super().__init__(
            "MashPy Codebase Agent",
            servers=[],
            agent_config=agent_config,
            **kwargs,
        )

    def register_commands(self, command_bus: CommandBus) -> None:
        """Register CodebaseAgent commands."""

        command_bus.register(
            Command(
                name="switch_repo",
                help="Switch to a local or GitHub repository.",
                handler=self._switch_repo_handler,
            )
        )
        command_bus.register(
            Command(
                name="current_repo",
                help="Show the active repository and tool mode.",
                handler=self._current_repo_handler,
            )
        )
        command_bus.register(
            Command(
                name="map_feature",
                help="Map a feature name to a code path in memory.",
                handler=self._map_feature_handler,
            )
        )

    def _switch_repo_handler(self, ctx: CLIContext, args: List[str]) -> None:
        target = " ".join(args).strip()
        if not target:
            ctx.renderer.warn("Usage: /switch_repo <path|url>")
            return
        if target.startswith(
            ("https://github.com/", "http://github.com/", "github.com/")
        ):
            self._switch_to_github(ctx, target)
            return
        path = os.path.expanduser(target)
        if os.path.isdir(path):
            self._switch_to_local(ctx, path)
            return
        ctx.renderer.error(f"Invalid path or URL: {target}")

    def _current_repo_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        if self.current_repo_type == "none":
            ctx.renderer.warn("No repository selected. Use /switch_repo.")
            return
        ctx.renderer.info(
            f"Current repo ({self.current_repo_type}): {self.current_repo_path}"
        )
        if self.current_repo_type == "local":
            ctx.renderer.info("Bash tool enabled.")
        elif self.current_repo_type == "github":
            ctx.renderer.info("GitHub MCP server connected.")

    def _map_feature_handler(self, ctx: CLIContext, args: List[str]) -> None:
        if len(args) < 2:
            ctx.renderer.warn("Usage: /map_feature <name> <path>")
            return
        feature_name = args[0].strip()
        path = " ".join(args[1:]).strip()
        if not feature_name or not path:
            ctx.renderer.warn("Usage: /map_feature <name> <path>")
            return
        app_id = self.agent_config.app_id if self.agent_config else self.app_name
        prefs = ctx.memory.get_preferences(app_id, ctx.session_id)
        if not isinstance(prefs, dict):
            prefs = {}
        feature_map = prefs.get("feature_map")
        if not isinstance(feature_map, dict):
            feature_map = {}
        feature_map[feature_name] = path
        prefs["feature_map"] = feature_map
        ctx.memory.set_preferences(app_id, ctx.session_id, prefs)
        ctx.renderer.info(f"Mapped feature '{feature_name}' -> {path}")

    def _switch_to_local(self, ctx: CLIContext, path: str) -> None:
        self._disconnect_github(ctx)
        repo_path = os.path.abspath(path)
        self.current_repo_path = repo_path
        self.current_repo_type = "local"
        if ctx.agent:
            ctx.agent.use_bash_tool = True
            ctx.agent.bash_working_dir = repo_path
        self._update_agent_context(ctx)
        self._refresh_tool_registry(ctx)
        ctx.renderer.info(f"Switched to local repository: {repo_path}")
        ctx.renderer.info("Bash tool enabled.")

    def _switch_to_github(self, ctx: CLIContext, raw: str) -> None:
        repo = self._parse_github_repo(raw)
        if repo is None:
            ctx.renderer.error(f"Invalid GitHub repo URL: {raw}")
            return
        if not GITHUB_MCP_PAT:
            ctx.renderer.error(
                "GITHUB_MCP_PAT is not set. Add it to your .env file to use GitHub MCP."
            )
            return
        if not self._ensure_github_connection(ctx):
            return
        self.current_repo_path = repo
        self.current_repo_type = "github"
        if ctx.agent:
            ctx.agent.use_bash_tool = False
            ctx.agent.bash_working_dir = None
        self._update_agent_context(ctx)
        self._refresh_tool_registry(ctx)
        ctx.renderer.info(f"Switched to GitHub repository: {repo}")
        ctx.renderer.info("GitHub MCP server connected.")

    def _ensure_github_connection(self, ctx: CLIContext) -> bool:
        if self.connection_by_name(GITHUB_CONNECTION_NAME):
            return True
        if not GITHUB_MCP_URL:
            ctx.renderer.error("GITHUB_MCP_URL is not configured.")
            return False
        headers: Dict[str, str] = {}
        if GITHUB_MCP_PAT:
            headers["Authorization"] = f"Bearer {GITHUB_MCP_PAT}"
        self._connect_servers(
            [
                {
                    "name": GITHUB_CONNECTION_NAME,
                    "url": GITHUB_MCP_URL,
                    "description": "GitHub MCP server for repository inspection.",
                    "type": "http",
                    "headers": headers,
                }
            ]
        )
        if not self.connection_by_name(GITHUB_CONNECTION_NAME):
            ctx.renderer.error("Failed to connect to the GitHub MCP server.")
            return False
        return True

    def _disconnect_github(self, ctx: CLIContext) -> None:
        connection = self.connection_by_name(GITHUB_CONNECTION_NAME)
        if connection is None:
            return
        try:
            connection.client.close()
        except MCPClientError as exc:
            ctx.renderer.warn(f"Failed to close GitHub connection: {exc}")
        self._connections = [
            entry for entry in self._connections if entry is not connection
        ]

    def _refresh_tool_registry(self, ctx: CLIContext) -> None:
        if ctx.agent is None:
            return
        registry = self._build_tool_registry(ctx)
        self._tool_registry = registry
        ctx.agent.set_tool_registry(registry)
        ctx.agent.refresh_tools(ctx.session_id)

    def _update_agent_context(self, ctx: CLIContext) -> None:
        if self.agent_config is None:
            return
        self.agent_config.app_context = self._build_app_context()
        if ctx.agent:
            ctx.agent.refresh_prompt()

    def _parse_github_repo(self, raw: str) -> Optional[str]:
        value = raw.strip()
        if value.startswith("github.com/"):
            value = f"https://{value}"
        parsed = urlparse(value)
        if parsed.netloc.lower() != "github.com":
            return None
        path = parsed.path.strip("/")
        if not path:
            return None
        parts = path.split("/")
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        return f"{owner}/{repo}"

    def _build_system_prompt(self) -> str:
        return (
            "You are an expert code analysis assistant helping engineers, PMs, and "
            "designers understand how product features work by exploring codebases.\n\n"
            "IMPORTANT: When using bash tool, all commands run in the repository "
            "root directory automatically.\n\n"
            "TOOLS BY REPOSITORY TYPE:\n\n"
            "LOCAL REPOSITORIES (Bash Tool):\n"
            "- You have direct bash access\n"
            "- Working directory is set to repo root\n"
            "- Use ripgrep (rg) for fast search\n"
            "- Common patterns:\n"
            '  - rg -l "pattern" --type py\n'
            '  - rg "def function" -A 10\n'
            "  - tree -L 3 -I 'node_modules|__pycache__|.git'\n"
            "  - cat -n src/file.py\n"
            '  - git log --oneline --grep="feature"\n\n'
            "GITHUB REPOSITORIES (MCP Tools):\n"
            "- Use mcp_github_search_code\n"
            "- Use mcp_github_get_file_contents\n"
            "- Use mcp_github_list_commits\n\n"
            "EXPLORATION STRATEGY:\n"
            "1. Check get_preferences for feature->code mappings first\n"
            "2. Start broad (search/find) then narrow down (read specific files)\n"
            '3. For "how does X work": trace full flow from entry point\n'
            '4. For "where is X": provide exact file paths and line numbers\n'
            "5. Adapt to user role (engineer vs PM/designer)\n\n"
            "Be thorough but efficient. Don't read entire large files."
        )

    def _build_app_context(self) -> str:
        base = (
            "Commands: /switch_repo, /current_repo, /map_feature, /usage\n"
            "Memory tools: get_preferences, set_preferences, get_full_conversation\n"
        )
        if self.current_repo_type == "local" and self.current_repo_path:
            return (
                f"{base}\nCurrent: Local repo at {self.current_repo_path}\n"
                "Bash commands execute in repo root automatically.\n"
            )
        if self.current_repo_type == "github" and self.current_repo_path:
            return (
                f"{base}\nCurrent: GitHub repo {self.current_repo_path}\n"
                "Use mcp_github_* tools (search_code, get_file_contents, list_commits)\n"
            )
        return f"{base}\nNo repository selected. Use /switch_repo."


def main() -> int:
    """Entry point for launching the Codebase Agent."""

    try:
        CodebaseAgent().run()
        return 0
    except MCPClientError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

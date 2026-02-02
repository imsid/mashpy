import argparse
import sys
import traceback
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List

from apps.codebase.index import create_cached_files, index_handler
from apps.codebase.onboard import configure_handler
from apps.codebase.prompts import (
    build_base_prompt,
    build_repo_context,
    build_user_prefs_context,
)
from mash.cli.app import CLIContext, MashApp
from mash.cli.commands import Command
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider
from mash.mcp.client import MCPClientError
from mash.memory.store import MemoryStore, SQLiteStore
from mash.tools.base import Tool
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GITHUB_MCP_PAT, GITHUB_MCP_URL

APP_ID: str = "codebase-agent"
GITHUB_CONNECTION_NAME = "github"


class RepoType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class CodebaseAgent(MashApp):
    def __init__(self, repo: str, gh: str):
        self.app_id: str = APP_ID
        self.repo = repo
        self.gh = gh
        self.store = self.register_memory_store()
        self.tools = self.register_tools()
        self.cached_files = self.register_cached_files()
        self.agent = self.register_agent()

        super().__init__(
            app_name=APP_ID,
            agent=self.agent,
            store=self.store,
            cached_files=self.cached_files,
            mcp_servers=[
                {
                    "name": GITHUB_CONNECTION_NAME,
                    "url": GITHUB_MCP_URL,
                    "description": "GitHub MCP server for repository inspection",
                    "headers": {"Authorization": f"Bearer {GITHUB_MCP_PAT}"},
                    "allowed_tools": [
                        "search_pull_requests",
                        "list_pull_requests",
                        "pull_request_read",
                        "get_commit",
                        "list_commits",
                        "list_issues",
                        "issue_read",
                    ],
                }
            ],
            log_destination=CodebaseAgent.get_logger_destination(),
        )

    @staticmethod
    def get_logger_destination() -> Path:
        return Path.home() / ".mash" / "logs" / "codebase.jsonl"

    @staticmethod
    def get_llm_provider() -> LLMProvider:
        return AnthropicProvider(
            api_key=ANTHROPIC_API_KEY,
            app_id=APP_ID,
        )

    @staticmethod
    def get_system_prompt(
        repo: str, cached_files: List[str], user_prefs: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        base_prompt: str = build_base_prompt(repo)
        repo_context: str = build_repo_context(repo, cached_files)
        user_prefs_context: str = build_user_prefs_context(user_prefs)
        return [
            {
                "type": "text",
                "text": base_prompt,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": repo_context,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": user_prefs_context,
                "cache_control": {"type": "ephemeral"},
            },
        ]

    @staticmethod
    def get_local_tools() -> List[Tool]:
        bash_tool = BashTool()  # Start with no working dir
        return [bash_tool]

    def register_cached_files(self) -> List[str]:
        cached_files = create_cached_files(repo_path=self.repo)
        return cached_files

    def register_memory_store(self) -> MemoryStore:
        db_path = Path(__file__).resolve().with_name("codebase.db")
        store = SQLiteStore(str(db_path))
        return store

    def register_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        local_tools = CodebaseAgent.get_local_tools()
        for tool in local_tools:
            tools.register(tool)
        return tools

    def register_agent(self) -> Agent:
        llm = CodebaseAgent.get_llm_provider()
        user_prefs = self.store.get_latest_preferences(app_id=self.app_id) or {}
        system_prompt = CodebaseAgent.get_system_prompt(
            repo=self.repo, cached_files=self.cached_files, user_prefs=user_prefs
        )
        tools = self.tools
        config = AgentConfig(
            app_id=self.app_id,
            system_prompt=system_prompt,
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            conversation_history_turns=3,
            compaction_token_threshold=30000,
            tool_search_enabled=False,  # Enable Claude tool search
        )
        agent = Agent(llm=llm, tools=tools, config=config)
        return agent

    def register_commands(self) -> None:
        """Register codebase-specific commands."""

        self.register_command(
            Command(
                name="configure",
                help="Set onboarding preferences (role, focus, response style)",
                handler=configure_handler,
            )
        )
        self.register_command(
            Command(
                name="repo",
                help="Show the active repository and tool mode",
                handler=self._repo_handler,
            )
        )
        self.register_command(
            Command(
                name="index",
                help="Manage repository index (build|show)",
                handler=index_handler,
            )
        )

    def _repo_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        """Handle /repo command."""
        ctx.renderer.info(f"Repo : {self.repo}")
        ctx.renderer.info(f"Github : {self.gh}")


def main() -> int:
    """Entry point for CodebaseAgent."""
    app = None
    try:
        parser = argparse.ArgumentParser(description="Run the Codebase agent.")
        parser.add_argument("--repo", required=True, help="Local path to the repo")
        parser.add_argument("--gh", required=True, help="Remote github URL to the repo")
        args = parser.parse_args()

        app = CodebaseAgent(repo=args.repo, gh=args.gh)
        app.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except MCPClientError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if app:
            app.cleanup()


if __name__ == "__main__":
    sys.exit(main())

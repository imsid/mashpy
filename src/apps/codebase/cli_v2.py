"""Codebase Q&A agent CLI using new Mash architecture (v2)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

from mash.cli.app import CLIContext, MashApp
from mash.cli.commands import Command
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.memory.ranker import ExampleRanker
from mash.memory.signals import SignalCollector
from mash.memory.store import SQLiteStore
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL


class CodebaseAgentV2(MashApp):
    """Clean, composable codebase agent built on new Mash architecture.

    Compared to legacy (308 lines):
    - Much simpler: ~150 lines
    - Clear separation of concerns
    - Signals and feedback loops built-in
    - Easy to extend and test
    """

    def __init__(self) -> None:
        """Initialize CodebaseAgent v2."""
        # 1. Configure agent
        config = AgentConfig(
            app_id="codebase-agent-v2",
            system_prompt=self._build_system_prompt(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
        )

        # 2. Set up tools
        tools = ToolRegistry()
        self.bash_tool = BashTool()  # Start with no working dir
        tools.register(self.bash_tool)

        # 3. Set up signals (feedback loops!)
        signals = SignalCollector()
        signals.register_signal("tool_calls", lambda e: len(e["action"].tool_calls))
        signals.register_signal(
            "has_error",
            lambda e: 1
            if any(r.is_error for r in e.get("results", []))
            else 0,
        )

        # 4. Set up store and ranker
        db_path = Path(__file__).resolve().with_name("codebase_v2.db")
        store = SQLiteStore(str(db_path))

        ranker = ExampleRanker(
            store=store,
            signal_weights={
                "tool_calls": 0.4,  # Prefer efficient interactions
                "has_error": -0.6,  # Avoid examples with errors
            },
        )

        # 5. Create agent with LLM provider
        llm = AnthropicProvider(api_key=ANTHROPIC_API_KEY)
        agent = Agent(llm=llm, tools=tools, config=config)
        agent.set_signal_collector(signals)
        agent.set_ranker(ranker)

        # 6. Initialize MashApp
        super().__init__(
            app_name="CodebaseAgent v2",
            agent=agent,
            store=store,
        )

        # Track current repository
        self.current_repo_path: str | None = None
        self.current_repo_type = "none"

    def register_commands(self) -> None:
        """Register codebase-specific commands."""
        self.register_command(
            Command(
                name="switch_repo",
                help="Switch to a local repository path",
                handler=self._switch_repo_handler,
            )
        )

        self.register_command(
            Command(
                name="current_repo",
                help="Show the active repository",
                handler=self._current_repo_handler,
            )
        )

    def _switch_repo_handler(self, ctx: CLIContext, args: List[str]) -> None:
        """Handle /switch_repo command."""
        target = " ".join(args).strip()
        if not target:
            ctx.renderer.warn("Usage: /switch_repo <path>")
            return

        # Expand path
        path = os.path.expanduser(target)
        if not os.path.isdir(path):
            ctx.renderer.error(f"Invalid path: {target}")
            return

        # Update bash tool working directory
        repo_path = os.path.abspath(path)
        self.bash_tool.restart(working_dir=repo_path)

        # Update state
        self.current_repo_path = repo_path
        self.current_repo_type = "local"

        # Update system prompt
        self.agent.config.system_prompt = self._build_system_prompt()

        ctx.renderer.info(f"Switched to local repository: {repo_path}")
        ctx.renderer.info("Bash tool enabled.")

    def _current_repo_handler(self, ctx: CLIContext, args: List[str]) -> None:
        """Handle /current_repo command."""
        if self.current_repo_type == "none":
            ctx.renderer.warn("No repository selected. Use /switch_repo.")
            return

        ctx.renderer.info(f"Current repo: {self.current_repo_path}")
        ctx.renderer.info("Mode: bash tool enabled")

    def _build_system_prompt(self) -> str:
        """Build the system prompt with current repo context."""
        repo_context = self._build_repo_context()

        return f"""Codebase guidance: You are an expert code analysis assistant helping engineers,
PMs, and designers understand how product features work by exploring codebases.

{repo_context}

IMPORTANT: When using bash tool, all commands run in the repository root directory automatically.

EXPLORATION STRATEGY:
1. Start broad (search/find) then narrow down (read specific files)
2. For "how does X work": trace full flow from entry point
3. For "where is X": provide exact file paths and line numbers
4. Adapt to user role (engineer vs PM/designer)

Common bash patterns:
- rg -l "pattern" --type py  # Search for pattern in Python files
- rg "def function" -A 10    # Search with context
- cat -n src/file.py         # Read file with line numbers
- tree -L 3 -I 'node_modules|__pycache__|.git'  # Directory structure

Be thorough but efficient. Don't read entire large files.
"""

    def _build_repo_context(self) -> str:
        """Build repository context for system prompt."""
        if self.current_repo_type == "local" and self.current_repo_path:
            return f"""Current repo: local at {self.current_repo_path}
Use bash for repository inspection. Working directory is set to repo root."""

        return "No repository selected. Ask the user to run /switch_repo <path>."


def main() -> int:
    """Entry point for CodebaseAgent v2."""
    try:
        CodebaseAgentV2().run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

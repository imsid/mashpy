"""Codebase Q&A agent CLI using new Mash architecture."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from mash.cli.app import CLIContext, MashApp
from mash.cli.commands import Command
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.mcp import MCPClientError, MCPManager
from mash.memory.signals import SignalCollector
from mash.memory.store import SQLiteStore
from mash.tools.bash import BashTool
from mash.tools.mcp import MCPToolAdapter
from mash.tools.registry import ToolRegistry

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GITHUB_MCP_PAT, GITHUB_MCP_URL

GITHUB_CONNECTION_NAME = "GitHub"


class CodebaseAgent(MashApp):
    """Clean, composable codebase agent built on new Mash architecture.

    Supports:
    - Local repositories via bash tool
    - GitHub repositories via MCP GitHub server
    """

    def __init__(self) -> None:
        """Initialize CodebaseAgent."""
        # Track current repository (must be set before building prompt)
        self.current_repo_path: str | None = None
        self.current_repo_type = "none"

        # 1. Configure agent
        config = AgentConfig(
            app_id="codebase-agent",
            system_prompt=self._build_system_prompt(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            tool_search_enabled=True,  # Enable Claude tool search
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
            lambda e: 1 if any(r.is_error for r in e.get("results", [])) else 0,
        )

        # 4. Set up store and ranker
        db_path = Path(__file__).resolve().with_name("codebase.db")
        store = SQLiteStore(str(db_path))

        # 5. Create agent with LLM provider (logger will be set by MashApp)
        llm = AnthropicProvider(
            api_key=ANTHROPIC_API_KEY,
            app_id=config.app_id,
        )
        agent = Agent(llm=llm, tools=tools, config=config)
        agent.set_signal_collector(signals)

        # 6. Initialize MashApp with log destination
        log_destination = Path.home() / ".mash" / "logs" / "codebase.jsonl"
        super().__init__(
            app_name="CodebaseAgent",
            agent=agent,
            store=store,
            log_destination=log_destination,
        )

        # 7. Set up MCP manager with event logger from MashApp
        self.mcp_manager = MCPManager(
            default_model=ANTHROPIC_MODEL,
            event_logger=self.event_logger,
            session_id=self.session_id,
            app_id=config.app_id,
        )

    def register_commands(self) -> None:
        """Register codebase-specific commands."""
        self.register_command(
            Command(
                name="switch_repo",
                help="Switch to a local or GitHub repository",
                handler=self._switch_repo_handler,
            )
        )

        self.register_command(
            Command(
                name="current_repo",
                help="Show the active repository and tool mode",
                handler=self._current_repo_handler,
            )
        )

    def _switch_repo_handler(self, ctx: CLIContext, args: List[str]) -> None:
        """Handle /switch_repo command for both local and GitHub repos."""
        target = " ".join(args).strip()
        if not target:
            ctx.renderer.warn("Usage: /switch_repo <path|github-url>")
            return

        # Check if it's a GitHub URL
        if target.startswith(
            ("https://github.com/", "http://github.com/", "github.com/")
        ):
            self._switch_to_github(ctx, target)
            return

        # Otherwise treat as local path
        path = os.path.expanduser(target)
        if not os.path.isdir(path):
            ctx.renderer.error(f"Invalid path or URL: {target}")
            return

        self._switch_to_local(ctx, path)

    def _current_repo_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        """Handle /current_repo command."""
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

    def _switch_to_local(self, ctx: CLIContext, path: str) -> None:
        """Switch to a local repository."""
        # Disconnect GitHub if connected
        self._disconnect_github(ctx)

        # Update bash tool working directory
        repo_path = os.path.abspath(path)
        self.bash_tool.restart(working_dir=repo_path)

        # Update state
        self.current_repo_path = repo_path
        self.current_repo_type = "local"

        # Update system prompt
        self.agent.config.system_prompt = self._build_system_prompt()

        # Refresh tools (remove GitHub MCP tools if any)
        self._refresh_tools()

        ctx.renderer.info(f"Switched to local repository: {repo_path}")
        ctx.renderer.info("Bash tool enabled.")

    def _switch_to_github(self, ctx: CLIContext, raw: str) -> None:
        """Switch to a GitHub repository."""
        repo = self._parse_github_repo(raw)
        if repo is None:
            ctx.renderer.error(f"Invalid GitHub repo URL: {raw}")
            return

        if not GITHUB_MCP_PAT:
            ctx.renderer.error(
                "GITHUB_MCP_PAT is not set. Add it to your .env file to use GitHub MCP."
            )
            return

        # Connect to GitHub MCP server
        if not self._ensure_github_connection(ctx):
            return

        # Disable bash tool for GitHub repos
        self.bash_tool.restart(working_dir=None)

        # Update state
        self.current_repo_path = repo
        self.current_repo_type = "github"

        # Update system prompt
        self.agent.config.system_prompt = self._build_system_prompt()

        # Refresh tools to include GitHub MCP tools
        self._refresh_tools()

        ctx.renderer.info(f"Switched to GitHub repository: {repo}")
        ctx.renderer.info("GitHub MCP server connected.")

    def _ensure_github_connection(self, ctx: CLIContext) -> bool:
        """Ensure GitHub MCP server is connected."""
        # Check if already connected
        if GITHUB_CONNECTION_NAME in self.mcp_manager:
            return True

        if not GITHUB_MCP_URL:
            ctx.renderer.error("GITHUB_MCP_URL is not configured.")
            return False

        # Set up headers
        headers = {}
        if GITHUB_MCP_PAT:
            headers["Authorization"] = f"Bearer {GITHUB_MCP_PAT}"

        # Connect to GitHub MCP server (MCPManager will log events)
        try:
            self.mcp_manager.add_server(
                name=GITHUB_CONNECTION_NAME,
                url=GITHUB_MCP_URL,
                description="GitHub MCP server for repository inspection",
                headers=headers,
                allowed_tools=[
                    "search_code",
                    "search_pull_requests",
                    "get_file_contents",
                    "get_commit",
                    "list_commits",
                    "list_pull_requests",
                    "pull_request_read",
                    "list_issues",
                    "issue_read",
                ],
                auto_connect=True,
            )
            return True
        except MCPClientError as e:
            ctx.renderer.error(f"Failed to connect to GitHub MCP server: {e}")
            return False

    def _disconnect_github(self, ctx: CLIContext) -> None:
        """Disconnect from GitHub MCP server."""
        if GITHUB_CONNECTION_NAME in self.mcp_manager:
            try:
                # MCPManager will log the disconnection event
                self.mcp_manager.remove_server(GITHUB_CONNECTION_NAME)
            except Exception as e:
                ctx.renderer.warn(f"Failed to disconnect GitHub: {e}")

    def _refresh_tools(self) -> None:
        """Refresh the tool registry with current tools."""
        # Clear existing tools except bash and runtime tools
        new_registry = ToolRegistry()

        # Only register bash for local repos (not for GitHub repos)
        if self.current_repo_type == "local":
            new_registry.register(self.bash_tool)

        # Preserve runtime tools (they start with get_, set_, list_, delete_)
        runtime_tool_names = [
            "get_conversation",
            "get_preferences",
            "set_preferences",
            "get_app_data",
            "set_app_data",
            "list_app_data",
            "delete_app_data",
        ]
        for tool_name in runtime_tool_names:
            tool = self.agent.tools.get(tool_name)
            if tool:
                new_registry.register(tool)

        # Add GitHub MCP tools if connected
        if GITHUB_CONNECTION_NAME in self.mcp_manager:
            mcp_tools = self.mcp_manager.get_flattened_tools(prefix="mcp_")

            for mcp_tool in mcp_tools:
                # Extract metadata
                server_name = mcp_tool.get("metadata", {}).get("server")
                original_name = mcp_tool.get("metadata", {}).get("original_name")

                if not server_name or not original_name:
                    continue

                # Create executor
                def make_executor(srv_name: str, tool_name: str):
                    def executor(args):
                        try:
                            result = self.mcp_manager.call_tool(
                                srv_name, tool_name, args
                            )
                            # Extract text content from MCP result
                            if isinstance(result, dict):
                                content = result.get("content", [])
                                if content and isinstance(content, list):
                                    texts = []
                                    for item in content:
                                        if isinstance(item, dict):
                                            texts.append(item.get("text", ""))
                                        elif isinstance(item, str):
                                            texts.append(item)
                                    return "\n".join(texts) if texts else str(result)
                            return str(result)
                        except Exception as e:
                            return f"Error: {e}"

                    return executor

                # Create and register adapter
                adapter = MCPToolAdapter.from_mcp_tool(
                    mcp_tool=mcp_tool,
                    executor=make_executor(server_name, original_name),
                    prefix="",  # Already prefixed
                )
                new_registry.register(adapter)

        # Update agent's tool registry
        self.agent.tools = new_registry

    def _parse_github_repo(self, raw: str) -> Optional[str]:
        """Parse a GitHub repository URL into owner/repo format."""
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
        """Build the system prompt with current repo context."""
        repo_context = self._build_repo_context()

        return f"""Codebase guidance: You are an expert code analysis assistant helping engineers,
PMs, and designers understand how product features work by exploring codebases.

**Session Start Protocol** (CRITICAL - Do this BEFORE exploring code):
1. Call get_preferences to check for stored user context
2. Call list_app_data to see what codebase knowledge has been accumulated
3. If relevant stored context exists (e.g., file locations, patterns, architecture):
   - Call get_app_data to retrieve it
   - Use this context to inform your exploration strategy
   - Avoid re-discovering what's already known
4. If preferences exist, adapt your communication style immediately
5. If no preferences and user shares their role/preferences: Call set_preferences IMMEDIATELY
6. If no preferences and user hasn't shared: Briefly ask (this is optional)

**When to Use set_app_data** (Build knowledge over time):
Store discoveries that will be useful in future queries:
- File locations: "authentication_files" → ["src/auth/login.py", "src/auth/middleware.py"]
- Architecture patterns: "api_structure" → "REST endpoints in src/api/, models in src/models/"
- Key components: "payment_flow" → "Starts in checkout.py:process_payment, calls stripe_client.py"
- Important configs: "database_setup" → "PostgreSQL config in config/db.py"
- Entry points: "app_entry_point" → "main.py:main() initializes FastAPI app"

DON'T store:
- Temporary information only relevant to current query
- Full file contents (just store locations/summaries)
- Information that changes frequently

**When User Shares Preferences**: Call set_preferences BEFORE responding!
Example: User says "I'm a PM" → First call set_preferences({{"role": "PM"}}), THEN respond

{repo_context}

IMPORTANT: When using bash tool, all commands run in the repository root directory automatically.

AVAILABLE TOOLS:

1. REPOSITORY TOOLS (context-dependent):

   LOCAL REPOSITORIES (Bash Tool):
   - You have direct bash access
   - Working directory is set to repo root
   - Use ripgrep (rg) for fast search
   - Common patterns:
     - rg -l "pattern" --type py
     - rg "def function" -A 10
     - tree -L 3 -I 'node_modules|__pycache__|.git'
     - cat -n src/file.py
     - git log --oneline --grep="feature"

   GITHUB REPOSITORIES (MCP Tools):
   - Use mcp_github_search_code for searching
   - Use mcp_github_get_file_contents to read files
   - Use mcp_github_list_commits for commit history
   - Use mcp_github_search_pull_requests for PRs
   - Use mcp_github_list_issues for issues

2. MEMORY & PERSISTENCE TOOLS (always available):
   - get_conversation, get_preferences, set_preferences
   - get_app_data, set_app_data, list_app_data, delete_app_data

   **Usage Pattern**:
   - START OF QUERY: Call list_app_data to see accumulated knowledge
   - DURING EXPLORATION: When you discover important locations/patterns, use set_app_data
   - FUTURE QUERIES: Retrieve stored context first to avoid redundant exploration

   **What to store with set_app_data**:
   - File locations for features ("auth_files", "payment_handlers", "api_endpoints")
   - Architecture summaries ("app_structure", "data_flow")
   - Key entry points ("main_entry", "api_gateway")
   - Important patterns or conventions found in the codebase

EXPLORATION STRATEGY:
1. CHECK STORED CONTEXT FIRST: list_app_data → get_app_data for relevant keys
2. Start broad (search/find) then narrow down (read specific files)
3. For "how does X work": trace full flow from entry point
4. For "where is X": Check stored context first, then search if needed
5. STORE DISCOVERIES: When you find important locations/patterns, use set_app_data
6. Adapt to user role and stored preferences:
   - PM: High-level features, business value, user impact
   - Engineer: Technical details, architecture, implementation patterns
   - Designer: User flows, UI/UX considerations, interaction patterns

Be thorough but efficient. Don't read entire large files. Build knowledge incrementally.
"""

    def _build_repo_context(self) -> str:
        """Build repository context for system prompt."""
        if self.current_repo_type == "local" and self.current_repo_path:
            return f"""Current repo: local at {self.current_repo_path}

**BASH EXPLORATION BEST PRACTICES**:

1. SEARCH-FIRST STRATEGY (for "what/where/which" questions):

   When user asks "what is X" or "where is Y" or "which files have Z":

   ✅ DO THIS FIRST:
     - rg -i "keyword" -C 3 | head -50              # Search with context
     - rg -l "keyword" --type py --type js          # Find relevant files
     - rg "class|function.*keyword" -A 5 | head -30 # Find definitions

   ❌ DON'T START WITH:
     - tree or ls (exploration is for later)
     - cat README.md (unless asked for overview)
     - Reading random files hoping to find it

   EXAMPLES:
     "what information is on company profile page"
       → rg -i "company.*profile|profile.*company" -C 5 | head -100
       → rg -l "profile.*page|ProfilePage" --type py --type js

     "where is authentication implemented"
       → rg -l "authenticate|def.*login|class.*Auth" --type py
       → rg "authenticate" -A 10 --type py | head -50

     "which files handle payments"
       → rg -l "payment|stripe|checkout" --type py --type js

2. START SMART (for exploration/overview questions):
   - README.md, CONTRIBUTING.md for overview
   - setup.py, pyproject.toml, package.json for dependencies/structure
   - Main entry points (main.py, app.py, index.js, etc.)

3. SEARCH EFFICIENTLY (use targeted commands):
   ✅ GOOD:
     - rg -l "class.*Model" --type py           # Find files only
     - rg "def authenticate" -A 10 --type py    # Show context
     - find . -name "test_*.py" -type f | head -20   # Limit output
     - tree -L 2 -I 'node_modules|__pycache__|.git'  # Shallow tree

   ❌ AVOID:
     - ls -laR                                  # Too much output
     - cat large_file.py                        # Read entire large files
     - find . -name "*.py"                      # Unlimited results

4. SKIP IRRELEVANT (don't waste tokens on):
   - Tests (unless specifically asked)
   - Migrations, fixtures, mocks
   - node_modules, __pycache__, .git, venv, dist, build
   - Generated files, minified code

5. TRUNCATE LARGE OUTPUTS (always limit results):
   - cat file.py | head -50    # First 50 lines
   - cat file.py | tail -30    # Last 30 lines
   - rg "pattern" | head -20   # Limit grep results
   - NEVER output more than 100 lines without truncation

6. USE STRUCTURE FOR EXPLORATION:
   - tree -L 2 -I 'tests|__pycache__|.git' → Get structure
   - rg -l "pattern" --type py → Find relevant files
   - cat specific_file.py → Read targeted files

Working directory is set to repo root. All bash commands execute there."""

        if self.current_repo_type == "github" and self.current_repo_path:
            return f"""Current repo: GitHub {self.current_repo_path}
Use mcp_github_* tools for repository inspection."""

        return "No repository selected. Ask the user to run /switch_repo <path|github-url>."

    def cleanup(self) -> None:
        """Clean up resources on shutdown."""
        # Disconnect all MCP servers (will log disconnection events)
        self.mcp_manager.disconnect_all()


def main() -> int:
    """Entry point for CodebaseAgent."""
    app = None
    try:
        app = CodebaseAgent()
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

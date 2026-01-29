"""Codebase Q&A agent CLI using new Mash architecture."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from prompt_toolkit import prompt

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
            system_prompt=self._build_system_prompt(ctx=None),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            conversation_history_turns=3,
            compaction_token_threshold=30000,
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
                name="configure",
                help="Set onboarding preferences (role, focus, response style)",
                handler=self._configure_handler,
            )
        )

        self.register_command(
            Command(
                name="current_repo",
                help="Show the active repository and tool mode",
                handler=self._current_repo_handler,
            )
        )

        self.register_command(
            Command(
                name="index",
                help="Manage repository index (build|show)",
                handler=self._index_handler,
            )
        )

    def _switch_repo_handler(self, ctx: CLIContext, args: List[str]) -> None:
        """Handle /switch_repo command for both local and GitHub repos."""
        target = " ".join(args).strip()
        if not target:
            ctx.renderer.warn("Usage: /switch_repo <path|github-url>")
            return

        switched = False

        # Check if it's a GitHub URL
        if target.startswith(
            ("https://github.com/", "http://github.com/", "github.com/")
        ):
            switched = self._switch_to_github(ctx, target)
        else:
            # Otherwise treat as local path
            path = os.path.expanduser(target)
            if not os.path.isdir(path):
                ctx.renderer.error(f"Invalid path or URL: {target}")
                return

            switched = self._switch_to_local(ctx, path)

        if switched:
            self._maybe_run_onboarding(ctx)

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

    def _switch_to_local(self, ctx: CLIContext, path: str) -> bool:
        """Switch to a local repository."""
        # Disconnect GitHub if connected
        self._disconnect_github(ctx)

        # Update bash tool working directory
        repo_path = os.path.abspath(path)
        self.bash_tool.restart(working_dir=repo_path)

        # Update state
        self.current_repo_path = repo_path
        self.current_repo_type = "local"

        # Generate repo index if not exists and populate cached_files
        self._ensure_repo_index(ctx, repo_path)

        # Update system prompt (NOW with ctx to access cached_files)
        self.agent.config.system_prompt = self._build_system_prompt(ctx)

        # Refresh tools (remove GitHub MCP tools if any)
        self._refresh_tools()

        # Disable tool search for local repos
        self.agent.config.tool_search_enabled = False

        ctx.renderer.info(f"Switched to local repository: {repo_path}")
        ctx.renderer.info("Bash tool enabled.")
        return True

    def _switch_to_github(self, ctx: CLIContext, raw: str) -> bool:
        """Switch to a GitHub repository."""
        repo = self._parse_github_repo(raw)
        if repo is None:
            ctx.renderer.error(f"Invalid GitHub repo URL: {raw}")
            return False

        if not GITHUB_MCP_PAT:
            ctx.renderer.error(
                "GITHUB_MCP_PAT is not set. Add it to your .env file to use GitHub MCP."
            )
            return False

        # Connect to GitHub MCP server
        if not self._ensure_github_connection(ctx):
            return False

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
        return True

    def _configure_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        """Handle /configure command for onboarding preferences."""
        self._run_preference_onboarding(ctx, allow_skip=True)

    def _maybe_run_onboarding(self, ctx: CLIContext) -> None:
        """Run onboarding if no preferences are set."""
        if not self.store:
            return

        prefs = self.store.get_preferences(
            app_id=self.agent.config.app_id,
            session_id=self.session_id,
        )
        if prefs:
            return

        ctx.renderer.info(
            "No preferences set yet. Let's answer three quick onboarding questions."
        )
        self._run_preference_onboarding(ctx, allow_skip=True)

    def _run_preference_onboarding(self, ctx: CLIContext, allow_skip: bool) -> None:
        """Collect deterministic onboarding preferences."""
        if not self.store:
            ctx.renderer.warn("Preferences store not available.")
            return

        current = (
            self.store.get_preferences(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
            )
            or {}
        )
        updated = dict(current)
        updated.pop("detail_level", None)

        if current:
            ctx.renderer.info("Press Enter to keep current selections.")

        role_options = [
            "Engineer",
            "Product Manager",
            "Designer",
            "Data/Analyst",
            "Other/Not specified",
        ]

        focus_options = [
            "Architecture and system design",
            "Feature behavior and user flows",
            "Implementation details and code",
            "Debugging and troubleshooting",
            "Performance and scalability",
        ]

        response_options = [
            "Concise, high-level summary with key takeaways",
            "Balanced mix of overview and technical detail",
            "Detailed, technical explanation with implementation steps",
            "Visual/structured response with bullet lists and diagrams where helpful",
        ]

        cancel = object()

        role_choice = self._prompt_choice(
            ctx,
            "1) What's your role?",
            role_options,
            default_value=current.get("role"),
            allow_skip=allow_skip,
            cancel_token=cancel,
        )
        if role_choice is cancel:
            ctx.renderer.warn("Preferences setup cancelled.")
            return
        if role_choice is not None:
            updated["role"] = role_choice

        focus_choice = self._prompt_choice(
            ctx,
            "2) What's your primary focus when asking about code?",
            focus_options,
            default_value=current.get("focus"),
            allow_skip=allow_skip,
            cancel_token=cancel,
        )
        if focus_choice is cancel:
            ctx.renderer.warn("Preferences setup cancelled.")
            return
        if focus_choice is not None:
            updated["focus"] = focus_choice

        style_choice = self._prompt_choice(
            ctx,
            "3) How should I respond?",
            response_options,
            default_value=current.get("style"),
            allow_skip=allow_skip,
            cancel_token=cancel,
        )
        if style_choice is cancel:
            ctx.renderer.warn("Preferences setup cancelled.")
            return
        if style_choice is not None:
            updated["style"] = style_choice

        if updated == current:
            ctx.renderer.info("Preferences unchanged.")
            return

        self.store.set_preferences(
            app_id=self.agent.config.app_id,
            session_id=self.session_id,
            preferences=updated,
        )

        ctx.renderer.info("Preferences saved.")
        rows = [
            ["role", updated.get("role", "(unset)")],
            ["focus", updated.get("focus", "(unset)")],
            ["style", updated.get("style", "(unset)")],
        ]
        ctx.renderer.table(["Preference", "Value"], rows)

    def _prompt_choice(
        self,
        ctx: CLIContext,
        question: str,
        options: List[str],
        default_value: Any,
        allow_skip: bool,
        cancel_token: object,
    ) -> Any:
        """Prompt for a numbered choice and return the mapped value."""
        ctx.renderer.info(question)
        rows = [[str(idx + 1), label] for idx, label in enumerate(options)]
        ctx.renderer.table(["#", "Option"], rows)

        default_index = None
        if default_value is not None:
            for idx, value in enumerate(options):
                if value == default_value:
                    default_index = idx
                    break

        prompt_parts = [f"Select 1-{len(options)}"]
        if default_index is not None:
            prompt_parts.append(f"Enter to keep current ({default_index + 1})")
        if allow_skip:
            prompt_parts.append("s to skip")
        prompt_parts.append("q to cancel")
        prompt_text = " / ".join(prompt_parts) + ": "

        while True:
            try:
                response = prompt(prompt_text).strip()
            except (EOFError, KeyboardInterrupt):
                return cancel_token

            if response == "" and default_index is not None:
                return options[default_index][1]
            if response.lower() in ("q", "quit"):
                return cancel_token
            if allow_skip and response.lower() in ("s", "skip"):
                return None
            if response.isdigit():
                choice = int(response)
                if 1 <= choice <= len(options):
                    return options[choice - 1]
            ctx.renderer.warn(
                f"Please enter a number between 1 and {len(options)}."
            )


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

    def _ensure_repo_index(self, ctx: CLIContext, repo_path: str) -> None:
        """Ensure repo index exists, generate if needed, and populate cached_files."""

        # Clear any previous cached files
        ctx.cached_files = []

        # Get git SHA and cache paths
        sha, cache_dir = self._get_cache_info(repo_path)
        if not sha or not cache_dir:
            return

        # Check if index exists
        repomap_json = cache_dir / "repomap.json"
        tags_file = cache_dir / "tags"

        if repomap_json.exists() and tags_file.exists():
            ctx.renderer.info(f"Repository index found (SHA: {sha[:7]})")
            # Populate cached_files
            ctx.cached_files = [str(repomap_json), str(tags_file)]
            return

        # Generate index
        ctx.renderer.info("Generating repository index (first time)...")
        success = self._run_repomap_script(ctx, repo_path, force=False)

        if success and repomap_json.exists() and tags_file.exists():
            ctx.renderer.info("Repository index generated successfully")
            # Populate cached_files
            ctx.cached_files = [str(repomap_json), str(tags_file)]
        else:
            ctx.renderer.warn("Failed to generate complete index")

    def _get_cache_info(self, repo_path: str) -> tuple[Optional[str], Optional[Path]]:
        """Get git SHA and cache directory for a repo.

        Returns:
            (sha, cache_dir) or (None, None) if not a git repo
        """

        try:
            # Check if we're in a git repo
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return None, None

            # Get SHA
            sha_result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if sha_result.returncode != 0:
                return None, None

            sha = sha_result.stdout.strip()
            repo_name = Path(repo_path).name
            cache_dir = Path.home() / ".mash" / "cache" / "repomap" / repo_name / sha

            return sha, cache_dir

        except Exception:
            return None, None

    def _run_repomap_script(
        self, ctx: CLIContext, repo_path: str, force: bool = False
    ) -> bool:
        """Run repomap.sh script to generate index.

        Args:
            ctx: CLI context
            repo_path: Path to repository
            force: If True, use --force flag to rebuild

        Returns:
            True if successful, False otherwise
        """

        script_path = Path(__file__).parent / "repomap.sh"

        try:
            args = ["bash", str(script_path)]
            if force:
                args.append("--force")
            args.append(repo_path)

            result = subprocess.run(
                args, capture_output=True, text=True, timeout=120, check=False
            )

            if result.returncode == 0:
                return True
            else:
                ctx.renderer.warn(f"repomap.sh failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            ctx.renderer.warn("Index generation timed out")
            return False
        except Exception as e:
            ctx.renderer.warn(f"Failed to run repomap.sh: {e}")
            return False

    def _refresh_tools(self) -> None:
        """Refresh the tool registry with current tools."""
        # Clear existing tools except bash and runtime tools
        new_registry = ToolRegistry()

        # Only register bash for local repos (not for GitHub repos)
        if self.current_repo_type == "local":
            new_registry.register(self.bash_tool)

        # Preserve runtime tools (they start with get_, set_, list_, delete_, load_)
        runtime_tool_names = [
            "get_conversation",
            "get_preferences",
            "set_preferences",
            "get_app_data",
            "set_app_data",
            "list_app_data",
            "delete_app_data",
            "load_cached_files",
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

    def _build_system_prompt(
        self, ctx: Optional[CLIContext] = None
    ) -> str | List[Dict[str, Any]]:
        """Build the system prompt with current repo context.

        Args:
            ctx: CLI context (used to access cached_files for index instructions)
        """
        repo_context, repomap_text = self._build_repo_context(ctx)

        # Add index instructions for local repos
        index_instructions = ""
        if self.current_repo_type == "local" and ctx and ctx.cached_files:
            index_instructions = self._build_index_instructions(
                ctx.cached_files, repomap_preloaded=repomap_text is not None
            )

        system_prompt = f"""Codebase guidance: You are an expert code analysis assistant helping engineers,
PMs, and designers understand how product features work by exploring codebases.

**Session Start Protocol** (CRITICAL - Do this BEFORE exploring code):
1. Call get_preferences to check for stored user context
2. Call list_app_data to see what codebase knowledge has been accumulated
3. **FOR LOCAL REPOS: Repository index is preloaded below** (repomap.json)
   - Use it to orient before searching
   - It provides: directory structure, entrypoints, configs, symbol map
4. If relevant stored context exists (e.g., file locations, patterns, architecture):
   - Call get_app_data to retrieve it
   - Use this context to inform your exploration strategy
   - Avoid re-discovering what's already known
5. If preferences exist, adapt your communication style immediately
6. If no preferences and user shares their role/preferences: Call set_preferences IMMEDIATELY
7. If no preferences and user hasn't shared: Briefly ask (this is optional)

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

{index_instructions}

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
   - **load_cached_files** - Load cached files (repomap.json is preloaded for local repos)

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
        if repomap_text:
            repomap_block = {
                "type": "text",
                "text": f"REPOSITORY INDEX (repomap.json):\n{repomap_text}",
                "cache_control": {"type": "ephemeral"},
            }
            return [
                {"type": "text", "text": system_prompt},
                repomap_block,
            ]

        return system_prompt

    def _build_repo_context(
        self, ctx: Optional[CLIContext] = None
    ) -> tuple[str, Optional[str]]:
        """Build repository context for system prompt."""
        repomap_text = None
        repomap_path = None
        if self.current_repo_type == "local" and ctx and ctx.cached_files:
            repomap_path = next(
                (f for f in ctx.cached_files if f.endswith("repomap.json")), None
            )
            if repomap_path:
                try:
                    repomap_text = Path(repomap_path).read_text(encoding="utf-8")
                except Exception:
                    repomap_text = None

        if self.current_repo_type == "local" and self.current_repo_path:
            repomap_note = (
                f"\nRepository index: {repomap_path} (preloaded below)"
                if repomap_path
                else ""
            )
            return (
                f"""Current repo: local at {self.current_repo_path}{repomap_note}

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

Working directory is set to repo root. All bash commands execute there.""",
                repomap_text,
            )

        if self.current_repo_type == "github" and self.current_repo_path:
            return (
                f"""Current repo: GitHub {self.current_repo_path}
Use mcp_github_* tools for repository inspection.""",
                None,
            )

        return (
            "No repository selected. Ask the user to run /switch_repo <path|github-url>.",
            None,
        )

    def _index_handler(self, ctx: CLIContext, args: List[str]) -> None:
        """Handle /index command.

        Usage:
            /index build [repo_name]   - Build or rebuild index
            /index show [repo_name]    - Display index
        """
        if not args:
            ctx.renderer.error("Usage: /index build|show [repo_name]")
            return

        action = args[0]
        repo_name = args[1] if len(args) > 1 else None

        # Determine repo path
        if repo_name:
            # Custom repo name provided - need to map to path
            # For now, assume current repo only
            ctx.renderer.warn("Custom repo name not yet supported. Using current repo.")

        if self.current_repo_type != "local" or not self.current_repo_path:
            ctx.renderer.error("No local repository selected. Use /switch_repo first.")
            return

        if action == "build":
            self._build_index(ctx, self.current_repo_path)
        elif action == "show":
            self._show_index(ctx, self.current_repo_path)
        else:
            ctx.renderer.error(f"Unknown action: {action}. Use 'build' or 'show'.")

    def _build_index(self, ctx: CLIContext, repo_path: str) -> None:
        """Build or rebuild repository index."""

        ctx.renderer.info("Building repository index...")

        # Use unified script runner with force=True
        success = self._run_repomap_script(ctx, repo_path, force=True)

        if success:
            # Get cache info and show paths
            _, cache_dir = self._get_cache_info(repo_path)
            if cache_dir:
                ctx.renderer.info("✅ Repository index built successfully")
                ctx.renderer.info(f"  {cache_dir / 'repomap.json'}")
                ctx.renderer.info(f"  {cache_dir / 'repomap.md'}")
                ctx.renderer.info(f"  {cache_dir / 'tags'}")

                # Update cached_files in context
                ctx.cached_files = [
                    str(cache_dir / "repomap.json"),
                    str(cache_dir / "tags"),
                ]
        else:
            ctx.renderer.error("Failed to build index")

    def _show_index(self, ctx: CLIContext, repo_path: str) -> None:
        """Display repository index."""

        try:
            # Get current SHA
            sha_result = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if sha_result.returncode != 0:
                ctx.renderer.error("Failed to get git SHA")
                return

            sha = sha_result.stdout.strip()
            repo_name = Path(repo_path).name
            cache_dir = Path.home() / ".mash" / "cache" / "repomap" / repo_name / sha
            md_path = cache_dir / "repomap.json"
            _ = cache_dir / "tags"

            if not md_path.exists():
                ctx.renderer.warn(
                    "Repository index not found. Run '/index build' first."
                )
                return

            # Read and display markdown
            md_content = md_path.read_text(encoding="utf-8")
            ctx.renderer.info(f"\n{md_content}")

        except Exception as e:
            ctx.renderer.error(f"Failed to show index: {e}")

    def _build_index_instructions(
        self, cached_files: List[str], repomap_preloaded: bool
    ) -> str:
        """Build instructions for using the repository index (local repos only).

        Args:
            cached_files: List of cached file paths from CLIContext
        """
        if not cached_files:
            return ""

        # Find repomap.json and tags from cached_files
        repomap_json = next(
            (f for f in cached_files if f.endswith("repomap.json")), None
        )
        tags_file = next((f for f in cached_files if f.endswith("tags")), None)

        if not repomap_json:
            return ""

        if repomap_preloaded:
            return f"""
**REPOSITORY INDEX PRELOADED** (Local repos only):

The repomap.json index is included below from:
  {repomap_json}

The repomap.json index contains:
  - directory_overview: Complete folder structure with depth
  - entrypoints: Main entry files (main.py, cli.py, app.py, etc.)
  - configs: Configuration files (pyproject.toml, setup.py, etc.)
  - packages: Symbol map with sample symbols (classes, functions by package)
  - search_seeds: High-signal search queries for common patterns
  - anchors: README and other key document locations

{f'Tags file available: {tags_file}. Load with load_cached_files if you need ctags detail.' if tags_file else ''}

USE INDEX TO ORIENT:
  - Review directory_overview to understand project structure
  - Check entrypoints to find where execution starts
  - Scan packages to see major modules and their symbols
  - Read README if present (path in anchors.readme)

TARGETED EXPLORATION:
  - Use index to identify relevant directories/files
  - Then use bash/rg for detailed searches
  - Example: Index shows "src/mash/tools/" → search there with rg

BENEFITS:
  ✓ Get complete repo structure without token-heavy bash commands
  ✓ Find entrypoints and key files instantly
  ✓ See symbol map (classes/functions) before diving in
  ✓ Use search_seeds for high-value queries
"""

        return f"""
**REPOSITORY INDEX AVAILABLE** (Local repos only):

The repository has been indexed with structural metadata. Load it FIRST before searching.

STEP 1 - LOAD INDEX FILES:
  # Load the JSON index
  load_cached_files(file_path="{repomap_json}")

  {'# Load the ctags file (symbol definitions)' if tags_file else ''}
  {f'load_cached_files(file_path="{tags_file}")' if tags_file else ''}

  The repomap.json index contains:
  - directory_overview: Complete folder structure with depth
  - entrypoints: Main entry files (main.py, cli.py, app.py, etc.)
  - configs: Configuration files (pyproject.toml, setup.py, etc.)
  - packages: Symbol map with sample symbols (classes, functions by package)
  - search_seeds: High-signal search queries for common patterns
  - anchors: README and other key document locations

  {'The tags file is a ctags output with symbol definitions (functions, classes, etc.)' if tags_file else ''}

STEP 2 - USE INDEX TO ORIENT:
  - Review directory_overview to understand project structure
  - Check entrypoints to find where execution starts
  - Scan packages to see major modules and their symbols
  - Read README if present (path in anchors.readme)

STEP 3 - TARGETED EXPLORATION:
  - Use index to identify relevant directories/files
  - Then use bash/rg for detailed searches
  - Example: Index shows "src/mash/tools/" → search there with rg

BENEFITS:
  ✓ Get complete repo structure without token-heavy bash commands
  ✓ Find entrypoints and key files instantly
  ✓ See symbol map (classes/functions) before diving in
  ✓ Use search_seeds for high-value queries

After loading index, use bash tool for detailed file exploration.
"""

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

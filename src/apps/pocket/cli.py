"""Pocket MCP application built on the new Mash architecture."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List

from mash.cli.app import CLIContext, MashApp
from mash.cli.commands import Command
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.mcp import MCPClientError, MCPManager
from mash.memory.signals import SignalCollector
from mash.memory.store import SQLiteStore
from mash.tools.mcp import MCPToolAdapter
from mash.tools.registry import ToolRegistry

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, POCKET_MCP_TOKEN, POCKET_MCP_URL

POCKET_CONNECTION_NAME = "Pocket"


class PocketApp(MashApp):
    """Pocket MCP client for company discovery and concierge interactions.

    Provides access to:
    - Company search by natural language, domain, or name
    - Company profile retrieval with detailed information
    - Pocket Concierge for questions, feedback, and demos
    """

    def __init__(self) -> None:
        """Initialize PocketApp."""
        # 1. Configure agent
        config = AgentConfig(
            app_id="pocket",
            system_prompt=self._build_system_prompt(),
            model=ANTHROPIC_MODEL,
            max_steps=30,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            tool_search_enabled=True,
        )

        # 2. Set up empty tool registry (MCP tools added after connection)
        tools = ToolRegistry()

        # 3. Set up signals
        signals = SignalCollector()
        signals.register_signal("tool_calls", lambda e: len(e["action"].tool_calls))
        signals.register_signal(
            "has_error",
            lambda e: 1 if any(r.is_error for r in e.get("results", [])) else 0,
        )

        # 4. Set up store
        db_path = Path(__file__).resolve().with_name("pocket.db")
        store = SQLiteStore(str(db_path))

        # 5. Create agent with LLM provider
        llm = AnthropicProvider(
            api_key=ANTHROPIC_API_KEY,
            app_id=config.app_id,
        )
        agent = Agent(llm=llm, tools=tools, config=config)
        agent.set_signal_collector(signals)

        # 6. Initialize MashApp with log destination
        log_destination = Path.home() / ".mash" / "logs" / "pocket.jsonl"
        super().__init__(
            app_name="Pocket",
            agent=agent,
            store=store,
            log_destination=log_destination,
        )

        # 7. Set up MCP manager
        self.mcp_manager = MCPManager(
            default_model=ANTHROPIC_MODEL,
            event_logger=self.event_logger,
            session_id=self.session_id,
            app_id=config.app_id,
        )

        # 8. Auto-connect to Pocket MCP server on startup
        self._auto_connect_pocket()

    def _auto_connect_pocket(self) -> None:
        """Automatically connect to Pocket MCP server on startup."""
        if not POCKET_MCP_URL or not POCKET_MCP_TOKEN:
            print(
                "Warning: POCKET_MCP_URL or POCKET_MCP_TOKEN not configured. "
                "Set these in your .env file to use Pocket tools.",
                file=sys.stderr,
            )
            return

        # Build URL with token
        url_with_token = f"{POCKET_MCP_URL}?token={POCKET_MCP_TOKEN}"

        try:
            self.mcp_manager.add_server(
                name=POCKET_CONNECTION_NAME,
                url=url_with_token,
                description="Pocket MCP server for company discovery and concierge",
                allowed_tools=["search", "concierge", "company_profile"],
                auto_connect=True,
            )

            # Add MCP tools to agent
            self._refresh_tools()

            print("✓ Connected to Pocket MCP server")
        except MCPClientError as e:
            print(f"Failed to connect to Pocket MCP server: {e}", file=sys.stderr)

    def register_commands(self) -> None:
        """Register Pocket-specific commands."""
        self.register_command(
            Command(
                name="reconnect",
                help="Reconnect to Pocket MCP server",
                handler=self._reconnect_handler,
            )
        )

        self.register_command(
            Command(
                name="status",
                help="Show Pocket MCP connection status",
                handler=self._status_handler,
            )
        )

    def _reconnect_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        """Handle /reconnect command."""
        # Disconnect if connected
        if POCKET_CONNECTION_NAME in self.mcp_manager:
            try:
                self.mcp_manager.remove_server(POCKET_CONNECTION_NAME)
                ctx.renderer.info("Disconnected from Pocket MCP server")
            except Exception as e:
                ctx.renderer.warn(f"Error disconnecting: {e}")

        # Reconnect
        ctx.renderer.info("Reconnecting to Pocket MCP server...")
        self._auto_connect_pocket()

        if POCKET_CONNECTION_NAME in self.mcp_manager:
            ctx.renderer.info("✓ Reconnected successfully")
        else:
            ctx.renderer.error("Failed to reconnect")

    def _status_handler(self, ctx: CLIContext, _args: List[str]) -> None:
        """Handle /status command."""
        if POCKET_CONNECTION_NAME in self.mcp_manager:
            ctx.renderer.info("✓ Connected to Pocket MCP server")

            # Show available tools
            tools = self.mcp_manager.get_flattened_tools(prefix="mcp_")
            tool_names = [t.get("name", "unknown") for t in tools]
            ctx.renderer.info(f"Available tools: {', '.join(tool_names)}")
        else:
            ctx.renderer.warn("Not connected to Pocket MCP server")
            ctx.renderer.info("Run /reconnect to connect")

    def _refresh_tools(self) -> None:
        """Refresh the tool registry with MCP tools."""
        new_registry = ToolRegistry()

        # Preserve runtime tools
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

        # Add Pocket MCP tools if connected
        if POCKET_CONNECTION_NAME in self.mcp_manager:
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

    def _build_system_prompt(self) -> str:
        """Build the system prompt for Pocket agent."""
        return """Pocket Assistant: You help users discover companies and interact with Pocket Concierge.

**Session Start Protocol** (CRITICAL - Do this BEFORE exploring):
1. Call get_preferences to check for stored user context
2. Call list_app_data to see what company information has been accumulated
3. If relevant stored context exists (e.g., saved companies, search patterns):
   - Call get_app_data to retrieve it
   - Use this context to inform your responses
   - Avoid re-discovering what's already known
4. If preferences exist, adapt your communication style immediately
5. If no preferences and user shares their role/preferences: Call set_preferences IMMEDIATELY
6. If no preferences and user hasn't shared: Briefly ask (this is optional)

**When to Use set_app_data** (Build knowledge over time):
Store discoveries that will be useful in future queries:
- Saved companies: "favorite_companies" → [{"domain": "example.com", "name": "Example Inc", ...}]
- Search patterns: "recent_searches" → ["AI startups", "Series A companies in SF"]
- User interests: "focus_areas" → ["fintech", "B2B SaaS", "climate tech"]
- Companies to follow up: "follow_up" → [{"domain": "...", "reason": "..."}]

DON'T store:
- Temporary search results
- Full company profiles (just store key info/references)
- Information that changes frequently

**When User Shares Preferences**: Call set_preferences BEFORE responding!
Example: User says "I'm interested in AI startups" → First call set_preferences({"interests": ["AI", "startups"]}), THEN respond

AVAILABLE TOOLS:

1. POCKET MCP TOOLS:

   **search** - Find companies by natural language query, domain, or fuzzy name
   - Returns scored matches with summaries, location, stage metadata
   - Use for: "find companies like X", "search for AI startups", "companies in fintech"
   - Parameters:
     - query (string, required): Search query (company name, domain, or description)
     - max_results (integer, optional): Maximum number of results (default: 10)

   **company_profile** - Load full Pocket company profile
   - Returns comprehensive profile: summary, tags, timeline, concierge prompts, commands
   - Use for: Getting detailed information about a specific company
   - Parameters:
     - domain (string, required): Company domain (e.g., "openai.com")

   **concierge** - Ask Pocket Concierge about a company
   - Get answers, share feedback, request demos, flag feature ideas
   - Use for: Questions, feedback, demo requests for specific companies
   - Parameters:
     - domain (string, required): Company domain
     - question (string, required): Your question or message
     - intent (string, optional): Intent type (e.g., "demo", "feedback", "question")
     - context (string, optional): Additional context

2. MEMORY & PERSISTENCE TOOLS (always available):
   - get_conversation, get_preferences, set_preferences
   - get_app_data, set_app_data, list_app_data, delete_app_data

   **Usage Pattern**:
   - START OF QUERY: Call list_app_data to see accumulated knowledge
   - DURING SEARCH: When user shows interest in companies, use set_app_data
   - FUTURE QUERIES: Retrieve stored context to provide personalized responses

   **What to store with set_app_data**:
   - Companies user is interested in
   - Search patterns and preferences
   - User's focus areas and interests
   - Follow-up items

USAGE STRATEGY:
1. CHECK STORED CONTEXT FIRST: list_app_data → get_app_data for relevant keys
2. For company search: Use descriptive natural language queries
3. For detailed info: Get company_profile after search
4. For questions/feedback: Use concierge with appropriate intent
5. STORE DISCOVERIES: When user shows interest, use set_app_data
6. Adapt to user preferences and interests

Be helpful and concise. Focus on providing relevant company information efficiently.
"""

    def cleanup(self) -> None:
        """Clean up resources on shutdown."""
        # Disconnect all MCP servers
        self.mcp_manager.disconnect_all()


def main() -> int:
    """Entry point for Pocket app."""
    app = None
    try:
        app = PocketApp()
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

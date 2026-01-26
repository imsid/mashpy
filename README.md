# mashpy

MashPy is a Python framework for building agentic CLIs with MCP support. It provides a modular architecture for creating intelligent command-line applications powered by Claude and integrated with Model Context Protocol (MCP) servers.

## Architecture

The framework is organized into composable layers:

- **`src/mash/core/`** - Agent runtime with LLM providers, execution loop, and configuration
- **`src/mash/cli/`** - CLI framework with REPL, commands, rendering, and chain-of-thought display
- **`src/mash/tools/`** - Tool system including registry, base classes, bash tool, MCP adapters, and runtime tools
- **`src/mash/memory/`** - Conversation storage, preferences, app data, and signal collectors
- **`src/mash/mcp/`** - MCP client management, server connections, and tool integration
- **`src/mash/logging/`** - Structured event logging with JSONL traces for debugging and observability
- **`src/apps/`** - Reference applications built on the framework

## Key Features

- **Modular Agent Runtime** - Clean separation of concerns with pluggable LLM providers
- **Tool Search** - Claude's guided tool search for efficient tool selection from large catalogs
- **Runtime Tools** - Built-in memory tools (conversation history, preferences, app data storage)
- **MCP Integration** - First-class support for Model Context Protocol servers
- **Real-time Rendering** - Chain-of-thought display showing agent reasoning and tool execution
- **Prompt Caching** - Automatic caching of system prompts for cost efficiency
- **Signal-based Feedback** - Collect metrics and implement feedback loops during execution
- **Structured Logging** - JSONL event logs with full trace context for analysis

## How It Works

Apps subclass `MashApp`, which orchestrates the agent, tools, memory, and MCP connections:

1. **Agent** - Executes think-act loops using configured LLM provider (Anthropic)
2. **Tools** - Registry of available tools (bash, MCP tools, runtime memory tools)
3. **Memory** - SQLite-backed storage for conversations, preferences, and app data
4. **MCP Manager** - Maintains connections to MCP servers and adapts their tools
5. **CLI** - REPL loop routing user input to commands or agent
6. **Logger** - Records all events (agent traces, tool calls, LLM requests) to JSONL

## Quick Start

1. **Python 3.10+** required
2. **Install**: `pip install -e .` (or `uv pip install -e .`)
3. **Configure**: Create `.env` with your credentials:
   ```bash
   ANTHROPIC_API_KEY=your_key_here
   ANTHROPIC_MODEL=claude-haiku-4-5-20251001  # optional, defaults to haiku
   ```
4. **Launch an app**:
   ```bash
   uv run codebase-agent  # Local/GitHub code analysis
   uv run pocket-app      # Company discovery via Pocket MCP
   ```

### Common Commands

All MashPy apps provide these built-in commands:

- `/help` - List available commands
- `/history` - Show conversation history
- `/usage` - Display token usage and costs
- `/clear` - Clear screen
- `/exit` - Exit the app

Apps can register custom commands (e.g., `/switch_repo`, `/status`, `/reconnect`).

## Framework Components

### `src/mash/core/` - Agent Runtime

**Agent** (`agent.py`)
- Orchestrates think-act execution loop
- Manages tool calls and conversation state
- Emits structured trace events
- Supports max_steps, max_tokens configuration

**LLM Providers** (`llm.py`)
- `AnthropicProvider` - Claude API integration with prompt caching
- Streaming responses with tool use support
- Automatic cache control headers for system prompts

**Configuration** (`config.py`)
- `AgentConfig` - Agent settings (model, API key, system prompt, tool search)
- Clean separation of app configuration from runtime state

### `src/mash/cli/` - CLI Framework

**MashApp** (`app.py`)
- Base class for all Mash applications
- Wires together agent, tools, memory, MCP manager, and logger
- Provides REPL loop with command routing
- Manages lifecycle (startup, shutdown, cleanup)

**Commands** (`commands.py`)
- Command registration with handlers
- Built-in commands: `/help`, `/history`, `/usage`, `/clear`, `/exit`
- Apps can register custom commands via `register_commands()`

**Rendering** (`render.py`, `chain_renderer.py`)
- Rich console output with color and formatting
- Real-time chain-of-thought display during agent execution
- Shows tool calls, arguments, token usage, and timing

**REPL** (`repl.py`)
- Interactive prompt with history
- Routes slash commands to command handlers
- Routes queries to agent runtime

### `src/mash/tools/` - Tool System

**ToolRegistry** (`registry.py`)
- Central registry for all available tools
- Type-safe tool lookup and invocation
- Supports tool search for large catalogs

**BashTool** (`bash.py`)
- Persistent bash session for local repository exploration
- Working directory management
- Command execution with timeout and output handling

**MCPToolAdapter** (`mcp.py`)
- Adapts MCP server tools to framework tool interface
- Handles argument mapping and result formatting

**Runtime Tools** (`runtime.py`)
- `get_conversation` - Retrieve conversation history
- `get_preferences` / `set_preferences` - User preferences
- `get_app_data` / `set_app_data` / `list_app_data` / `delete_app_data` - Persistent storage

### `src/mash/memory/` - Storage

**SQLiteStore** (`store.py`)
- Conversation history with full message details
- User preferences (key-value)
- App data storage for building knowledge over time
- Thread-safe SQLite operations

**SignalCollector** (`signals.py`)
- Register custom signals for feedback loops
- Track metrics like tool calls, errors, token usage
- Use signals to implement adaptive behavior

### `src/mash/mcp/` - MCP Integration

**MCPManager** (`manager.py`)
- Add/remove MCP server connections
- Get flattened tool list from all servers
- Route tool calls to appropriate server
- Integrated with event logger

**MCPClient** (`client.py`)
- HTTP-based MCP client implementation
- Server handshake and initialization
- Tool listing and invocation

### `src/mash/logging/` - Event Logging

**EventLogger** (`logger.py`)
- JSONL event logging for full observability
- Event types: `agent.trace.*`, `llm.request.*`, `agent.tool.*`
- Automatic trace ID propagation
- Logs to `~/.mash/logs/{app}.jsonl`

**Events** (`events.py`)
- Structured event schemas: `AgentTraceEvent`, `LLMEvent`, `ToolEvent`
- Captures duration, token usage, tool calls, errors
- Enables post-hoc analysis and debugging

## Advanced Features

### Prompt Caching

The framework automatically adds cache control headers to system prompts, reducing costs for repeated queries:

```python
# System prompt is automatically cached
config = AgentConfig(
    app_id="myapp",
    system_prompt="Long system prompt...",  # Cached by AnthropicProvider
    model="claude-haiku-4-5-20251001",
)
```

Cache hits significantly reduce input token costs (10x cheaper for cached tokens).

### Tool Search

Enable Claude's guided tool search for efficient tool selection from large catalogs:

```python
config = AgentConfig(
    # ...
    tool_search_enabled=True,  # Enables tool_search_tool_bm25 beta
)
```

Tool search is especially useful when you have 10+ tools available.

### Signal-Based Feedback Loops

Use signals to track metrics and implement adaptive behavior:

```python
signals = SignalCollector()

# Track tool usage
signals.register_signal("tool_calls", lambda e: len(e["action"].tool_calls))

# Track errors
signals.register_signal(
    "has_error",
    lambda e: 1 if any(r.is_error for r in e.get("results", [])) else 0,
)

# Access signals during execution
agent.set_signal_collector(signals)
```

### Runtime Tools for Context Building

All apps automatically get runtime tools for building context over time:

- **Conversation History**: `get_conversation` - Retrieve past messages
- **Preferences**: `get_preferences`, `set_preferences` - Store user preferences
- **App Data**: `get_app_data`, `set_app_data`, `list_app_data`, `delete_app_data` - Build knowledge base

Example system prompt pattern:
```python
system_prompt = """
**Session Start Protocol**:
1. Call list_app_data to see accumulated knowledge
2. Call get_preferences to check user context
3. Use stored context to inform responses
4. Store new discoveries with set_app_data

When user shows interest in something, store it for future queries.
"""
```

### Event Logging and Tracing

All agent activity is logged to JSONL for debugging and analysis:

```python
# Logs automatically go to ~/.mash/logs/{app}.jsonl
# Events include:
# - agent.trace.start, agent.trace.complete
# - agent.think.complete, agent.act.complete
# - llm.request.start, llm.request.complete
# - agent.tool.call, agent.tool.result

# Each event includes trace_id, session_id, timing, and context
```

Use the logs to:
- Debug agent behavior
- Analyze token usage patterns
- Track tool call frequency
- Measure response times
- Identify errors and bottlenecks

## Reference Applications

### Codebase Agent (`codebase-agent`)

Code analysis assistant for engineers, PMs, and designers to understand how product features work by exploring codebases. Supports both local and GitHub repositories.

**Features:**
- Local repository exploration via bash tool
- GitHub repository access via MCP GitHub server
- Runtime switch between repos with `/switch_repo`
- Session memory for accumulated codebase knowledge
- Tool search enabled for efficient exploration

**Configuration:**
```bash
ANTHROPIC_API_KEY=your_key          # Required
ANTHROPIC_MODEL=claude-haiku-4-5-20251001  # Optional, defaults to haiku
GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/  # Optional
GITHUB_MCP_PAT=ghp_xxx             # Required for GitHub mode
```

**Usage:**
```bash
uv run codebase-agent
# Use /switch_repo to select local or GitHub repository
# Ask questions about the codebase
```

### Pocket App (`pocket-app`)

Company discovery agent connecting to Pocket MCP server for searching companies, viewing profiles, and interacting with Pocket Concierge.

**Features:**
- Company search by name, domain, or natural language
- Detailed company profiles
- Pocket Concierge for questions and demo requests
- Auto-connect to Pocket MCP server on startup
- Session memory for favorite companies and search patterns

**Configuration:**
```bash
ANTHROPIC_API_KEY=your_key          # Required
ANTHROPIC_MODEL=claude-haiku-4-5-20251001  # Optional, defaults to haiku
POCKET_MCP_URL=https://pocket-feed-mcp.onrender.com/mcp/pocket  # Optional
POCKET_MCP_TOKEN=your_token         # Required
```

**Commands:**
- `/status` - Show connection status and available tools
- `/reconnect` - Reconnect to Pocket MCP server

**Usage:**
```bash
uv run pocket-app
# Ask: "Find AI startups in SF"
# Ask: "Show me the profile for openai.com"
# Ask: "Request a demo for anthropic.com"
```

## Building Your Own App

Creating a new Mash application follows a clean, composable pattern:

### 1. Create app structure

```bash
mkdir -p src/apps/myapp
touch src/apps/myapp/__init__.py
touch src/apps/myapp/cli.py
touch src/apps/myapp/config.py
```

### 2. Configure your app (`config.py`)

```python
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
```

### 3. Implement your app (`cli.py`)

```python
from pathlib import Path
from mash.cli.app import MashApp, CLIContext
from mash.cli.commands import Command
from mash.core.agent import Agent
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.memory.store import SQLiteStore
from mash.memory.signals import SignalCollector
from mash.tools.registry import ToolRegistry

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL


class MyApp(MashApp):
    """Your custom Mash application."""

    def __init__(self) -> None:
        # 1. Configure agent
        config = AgentConfig(
            app_id="myapp",
            system_prompt="You are a helpful assistant.",
            model=ANTHROPIC_MODEL,
            max_steps=10,
            max_tokens=4096,
            api_key=ANTHROPIC_API_KEY,
            tool_search_enabled=True,
        )

        # 2. Set up tools
        tools = ToolRegistry()
        # Add your custom tools here

        # 3. Set up signals for feedback loops
        signals = SignalCollector()
        signals.register_signal("tool_calls", lambda e: len(e["action"].tool_calls))

        # 4. Set up memory store
        db_path = Path(__file__).resolve().with_name("myapp.db")
        store = SQLiteStore(str(db_path))

        # 5. Create agent with LLM provider
        llm = AnthropicProvider(api_key=ANTHROPIC_API_KEY, app_id=config.app_id)
        agent = Agent(llm=llm, tools=tools, config=config)
        agent.set_signal_collector(signals)

        # 6. Initialize MashApp
        log_destination = Path.home() / ".mash" / "logs" / "myapp.jsonl"
        super().__init__(
            app_name="MyApp",
            agent=agent,
            store=store,
            log_destination=log_destination,
        )

    def register_commands(self) -> None:
        """Register custom commands."""
        self.register_command(
            Command(
                name="ping",
                help="Check if app is responding",
                handler=self._ping_handler,
            )
        )

    def _ping_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Handle /ping command."""
        ctx.renderer.info("pong!")


def main() -> int:
    """Entry point."""
    try:
        app = MyApp()
        app.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
```

### 4. Add console script entry (`pyproject.toml`)

```toml
[project.scripts]
myapp = "apps.myapp.cli:main"
```

### 5. Install and run

```bash
pip install -e .
uv run myapp
```

### Adding MCP Connections

To integrate MCP servers:

```python
from mash.mcp import MCPManager

# In __init__:
self.mcp_manager = MCPManager(
    default_model=ANTHROPIC_MODEL,
    event_logger=self.event_logger,
    session_id=self.session_id,
    app_id=config.app_id,
)

# Add a server:
self.mcp_manager.add_server(
    name="MyMCPServer",
    url="https://example.com/mcp",
    description="My custom MCP server",
    allowed_tools=["tool1", "tool2"],
    auto_connect=True,
)

# Get flattened tools and add to agent:
mcp_tools = self.mcp_manager.get_flattened_tools(prefix="mcp_")
for mcp_tool in mcp_tools:
    adapter = MCPToolAdapter.from_mcp_tool(
        mcp_tool=mcp_tool,
        executor=lambda args: self.mcp_manager.call_tool(server_name, tool_name, args),
    )
    self.agent.tools.register(adapter)
```

## Development

### Project Structure

```
src/
├── mash/                    # Core framework
│   ├── core/               # Agent runtime, LLM providers, config
│   ├── cli/                # CLI framework, REPL, commands, rendering
│   ├── tools/              # Tool system (registry, bash, MCP adapters, runtime)
│   ├── memory/             # Storage (conversations, preferences, app data)
│   ├── mcp/                # MCP client management
│   └── logging/            # Event logging and tracing
├── apps/                    # Reference applications
│   ├── codebase/           # Codebase Q&A agent
│   └── pocket/             # Pocket MCP client
└── mash_legacy/            # Legacy implementation (deprecated)
```

### Testing



### Logging and Debugging

All apps log events to `~/.mash/logs/{app}.jsonl`. Analyze logs using standard JSONL tools:

```bash
# View all agent traces
cat ~/.mash/logs/codebase.jsonl | jq 'select(.event_type | startswith("agent.trace"))'

# Calculate total tokens used
cat ~/.mash/logs/codebase.jsonl | jq 'select(.event_type == "llm.request.complete") | .token_usage.total' | awk '{s+=$1} END {print s}'

# Find slow tool calls
cat ~/.mash/logs/codebase.jsonl | jq 'select(.event_type == "agent.tool.result" and .duration_ms > 1000)'
```



## mash

`mash` is the application layer for building MCP-aware CLIs. It provides a
REPL, slash command framework, optional agent runtime, local memory, and
structured logging + telemetry.

### Key components

- `Mash` (base class) wires the REPL, default commands, memory, logging, and MCP
  connections together.
- `CommandBus` registers slash commands and emits command lifecycle events.
- `CommandRouter` forwards `/commands` to the bus and routes other input to the
  agent when enabled.
- `AgentRuntime` orchestrates LLM calls, tool execution, and trace events (in
  `mashd`).
- `ToolRegistry` merges MCP server tools, command tools, and memory helpers (in
  `mashd`).
- `SqliteMemory` stores conversations, preferences, and app-specific data in a local SQLite file.
- `EventLogger` writes JSONL events to a configured destination.
- `TelemetryCollector` tracks token usage per session (in `mashd`).

### Mash base class

`Mash` takes an app name and a list of server configs (`name`, `url`, optional
`headers`, optional `tools` allowlist). It creates a `Host`, connects to MCP
servers, and launches the REPL.
Default storage/logging artifacts are created in the working directory:

- `.{slug}_memory.sqlite3` - conversation history, preferences, and app-specific data.
- `.{slug}.log` - JSONL event log.

Base commands registered automatically:

- `/help` - list available commands.
- `/exit` - terminate the session.
- `/list [server]` - list resources, templates, and tools.
- `/execute <server> <tool>` - call a tool with prompted arguments.
- `/app_data [key]` - show stored app data entries.

### Command framework

`CommandBus` normalizes names and aliases, executes handlers, and emits
`CommandEvent` telemetry. Commands can be exported as tools with
`Command.to_tool_spec`, which accepts a JSON schema for argument collection.

`CommandRouter` integrates agent mode: if a line is not a slash command, it is
sent to the `AgentRuntime` (when enabled) and recorded in memory.

### Agent mode

Agent mode is opt-in via `AgentConfig`:

```python
from mash import AgentConfig, Mash

class MyApp(Mash):
    def __init__(self, **kwargs):
        super().__init__(
            "My App",
            servers=MY_SERVERS,
            agent_config=AgentConfig(
                app_id="myapp",
                system_prompt="You are a helpful MCP assistant.",
                model="claude-haiku-4-5-20251001",
            ),
            **kwargs,
        )
```

`AgentConfig` fields:

- `app_id` - namespace for memory + logging.
- `system_prompt` - prompt scaffolding.
- `model`, `max_steps`, `max_tokens`, `max_history_messages`.
- `tool_search_enabled` - enable server-side tool search.
- `anthropic_api_key` - optional; defaults to `ANTHROPIC_API_KEY` if the SDK uses env.

When agent mode is active, `/usage` is registered to show token totals for the
current session via `TelemetryCollector`.

Tool search uses Claude's server-side tool search (`tool_search_tool_bm25`) with
deferred tool loading and requires the `advanced-tool-use-2025-11-20` beta flag.

### Tool naming and invocation

Tools exposed to the agent follow a predictable naming scheme:

- MCP tools: `mcp_<server>_<tool>` (normalized with `normalize_tool_name`).
- Command tools: `cmd_<command>` if you register them manually.
- Memory helpers: `get_full_conversation`, `get_preferences`, `set_preferences`,
  `list_app_data`, `set_app_data`.
- Tool search: `tool_search_tool_bm25` when enabled.

`/execute` prompts for arguments using the tool's JSON schema when available.
It supports:

- Standard `properties`/`required` schemas.
- Alternate schemas embedded under `content.text` containing JSON with
  `arguments` (used by some MCP servers).

### Memory and logging

- `SqliteMemory` isolates conversations/preferences by `app_id` + `session_id` and
  app-specific data by `app_id` + `session_id` + `key` to prevent leakage
  across apps or sessions.
- `EventLogger` writes JSON lines for `LogEvent`, `CommandEvent`, `DebugEvent`,
  and `AgentTraceEvent` with optional duration + trace metadata.

### Files

- `base.py` - core app wiring + default command handlers.
- `commands.py` - slash command system + tool export helpers.
- `router.py` - command/agent routing logic.
- `mashd/agent.py` - agent runtime and loop orchestration.
- `mashd/models.py` - agent runtime dataclasses.
- `mashd/llm_provider.py` - LLM provider abstraction + Anthropic adapter.
- `mashd/telemetry.py` - token usage tracking.
- `mashd/tools.py` - tool registry and invocation helpers.
- `memory.py` - SQLite memory for conversations, preferences, and app-specific data.
- `logging.py` - event logger + log event types.
- `mashd/README.md` - mashd module overview.

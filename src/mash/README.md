## mash

`mash` is the application/middleware layer for building MCP-aware CLIs.
It provides a REPL, command framework, optional agent runtime, memory, and
event logging + telemetry.

### Key components

- `Mash` (base class) wires the REPL, default commands, memory, and MCP
  connections together.
- `CommandBus` registers slash commands and can export them as tools.
- `AgentRuntime` orchestrates LLM calls, tool execution, and tracing.
- `ToolRegistry` merges local command tools and MCP server tools.
- `SqliteMemory` stores conversation + preference state.
- `EventLogger` writes JSON log events to the configured destination.
- `AgentTraceEvent` + `TelemetryCollector` capture agent traces and token usage.

### Agent mode

Agent mode is optional and configured per app:

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
                anthropic_api_key="sk-ant-...",
                mode="hybrid",
            ),
            **kwargs,
        )
```

Agent modes:
- `off` - disable agent behavior.
- `hybrid` - slash commands run normally, other input goes to the agent.
- `agent` - same as hybrid today, but reserved for future agent-first routing.

When enabled, the following command is available:
- `/usage` - show token usage for the current session.

Tool search uses Claude's server-side tool search (`tool_search_tool_bm25`)
with deferred tool loading, and requires the `advanced-tool-use-2025-11-20`
beta flag in the Anthropic client.

### Logging

Logs are emitted as JSON event lines to the destination configured by the app.
Events include `LogEvent`, `AgentTraceEvent`, `CommandEvent`, and `DebugEvent`.

### Tool naming

Tools exposed to the agent follow a predictable naming scheme:
- Commands: `cmd_<command>`
- MCP tools: `mcp_<server>_<tool>`
- Memory helpers: `get_full_conversation`, `get_preferences`, `set_preferences`
- Tool search: `tool_search_tool_bm25` (server-side tool search)

### Memory isolation

`SqliteMemory` stores conversations and preferences scoped by app + session,
so context stored by one app/session does not bleed into another.

### Files

- `base.py` - core wiring for apps.
- `commands.py` - slash command system.
- `agent.py` - agent runtime and Anthropic client wrapper.
- `tools.py` - tool registry and invocation helpers.
- `memory.py` - SQLite memory for conversations + preferences.
- `logging.py` - event logger + log event types.
- `telemetry.py` - token usage tracking.

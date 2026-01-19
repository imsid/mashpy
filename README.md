# mashpy

MashPy is a set of MCP-aware CLIs and a Python framework for building them. It is
split into three layers:

- `src/mash/` - CLI framework with REPL, slash commands, agent runtime, memory, and logging.
- `src/mashnet/` - MCP HTTP client + host (handshake, SSE, sampling/elicitation).
- `src/apps/` - Example CLIs built on top of `mash`.

## How the pieces connect

`apps/<app>` subclasses `mash.Mash`, which wires the REPL, command system, memory,
and MCP connections together. `Mash` uses `mashnet.Host` to maintain MCP client
instances and expose server tools/resources. In agent mode, `Mash` builds a
tool catalog from MCP tools plus memory helpers, then routes non-slash input to
the agent runtime.

## Quick start

1. Python 3.10+.
2. Install locally: `pip install -e .` (or `uv pip install -e .`).
3. Configure credentials in `.env` as needed (see app details below).
4. Launch a CLI:
   - Pocket CLI: `uv run pocket-app`
   - GitHub CLI: `uv run plog-app`

### Common commands

All MashPy apps share the base command set:

- `/help` - list available commands.
- `/list [server]` - show resources, templates, and tools.
- `/execute <server> <tool>` - call a tool, prompting for arguments.
- `/exit` - close the session.
- `/usage` - show token usage (only when agent mode is enabled).

## src/mash (CLI framework)

### Mash base class

`Mash` is the base class for MCP-aware apps. It:

- Connects to configured servers (`servers` is a list of dicts with `name`, `url`,
  and optional `headers`).
- Creates a `CommandBus`, renderer, event logger, and SQLite-backed memory.
- Starts a REPL loop and cleanly closes MCP connections on exit.
- Builds a tool registry from MCP tools for agent mode.

Default artifacts are created in the current working directory:

- `.{slug}_memory.sqlite3` - conversation + preferences (SQLite).
- `.{slug}.log` - JSONL event log.

### Commands and routing

- `CommandBus` registers slash commands (with optional aliases) and emits
  `CommandEvent` telemetry.
- `CommandRouter` routes `/commands` to the command bus and other input to the
  agent runtime when enabled.
- Commands can be exported as tools via `Command.to_tool_spec` if you want to
  include them in a custom tool registry.

### Agent runtime

Agent mode is opt-in by passing `AgentConfig` to `Mash`. Highlights:

- Uses the Anthropic SDK (`anthropic`) to call the configured model.
- Supports tool search via the `tool_search_tool_bm25` beta tool
  (`advanced-tool-use-2025-11-20`).
- Adds memory tools: `get_full_conversation`, `get_preferences`, `set_preferences`.
- Tracks token usage via `TelemetryCollector` and logs trace events.

Minimal example:

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

### Tool naming and invocation

Tools exposed to the agent are normalized for safety:

- MCP tools: `mcp_<server>_<tool>` (normalized with `normalize_tool_name`).
- Command tools: `cmd_<command>` if you register them manually.
- Memory helpers: `get_full_conversation`, `get_preferences`, `set_preferences`.

`/execute` prompts for arguments using the server-provided JSON schema when
available, otherwise it accepts a raw JSON object.

## src/apps (example CLIs)

### Pocket (`pocket-app`)

`PocketCLI` connects to the hosted Pocket MCP server and enables agent mode
by default. Configuration:

- `ANTHROPIC_API_KEY` - required for agent mode.
- `ANTHROPIC_MODEL` - optional; defaults to `claude-haiku-4-5-20251001`.

The app context describes Pocket tools such as `search`, `concierge`, and
`company_profile`. Logs are written to `src/apps/pocket/pocket.log`.

### GitHub (`plog-app`)

`PlogCLI` connects to GitHub's MCP server using a PAT. Configuration:

- `GITHUB_MCP_PAT` - required (set in `.env`).

Logs are written to `src/apps/plog/plog.log`. Agent mode is not enabled by
default for this app, but you can add it by passing an `AgentConfig` in a
subclass.

### Creating a new app

1. Create a package under `src/apps/<name>/`.
2. Define server configs (name, url, headers).
3. Subclass `mash.Mash` and register any custom commands.
4. Add a console script entry to `pyproject.toml`.

Minimal example:

```python
from mash import Mash
from mash.commands import Command, CommandBus

class MyApp(Mash):
    def __init__(self, **kwargs):
        super().__init__("My App", servers=MY_SERVERS, **kwargs)

    def register_commands(self, command_bus: CommandBus) -> None:
        command_bus.register(
            Command(
                name="ping",
                help="Check connectivity.",
                handler=lambda ctx, args: ctx.renderer.info("pong"),
            )
        )
```

To enable agent mode, pass `AgentConfig` and ensure `anthropic` is installed.

## src/mashnet (MCP transport)

`mashnet` handles the MCP HTTP transport:

- `MCPHTTPClient` performs the initialize/initialized handshake, listens for
  SSE messages, and exposes helpers like `list_tools` and `call_tool`.
- `Host` caches client instances and responds to sampling/elicitation requests.

Sampling requests are fulfilled via OpenAI's chat completion API. You can
override the default model with `PLOG_SAMPLING_MODEL`, and OpenAI credentials
are read from the usual environment variables (for example `OPENAI_API_KEY`).

## Module docs

- `src/mash/README.md` - CLI framework + agent runtime details.
- `src/mashnet/README.md` - MCP client/host details.
- `src/apps/README.md` - App layout and how to add new CLIs.

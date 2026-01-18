# mashpy

MashPy is a set of CLI apps and libraries for building workflows on top of
Model Context Protocol (MCP) servers. It is split into three layers:

- `src/mash/` provides the CLI framework and optional agent runtime.
- `src/mashnet/` provides the MCP client/host that talks to servers.
- `src/apps/` ships example CLIs built on top of `mash`.

## How the pieces connect

`apps/<app>` subclasses `mash.Mash`, which wires up the REPL, commands,
memory, and optional agent mode. `Mash` uses `mashnet.Host` to maintain
MCP server connections and surface tools/resources. In agent mode, `mash`
turns those tools (plus local commands and memory helpers) into a tool
catalog for the LLM to orchestrate.

## Quick start

1. Python 3.10+.
2. Install locally: `pip install -e .` (or `uv pip install -e .`).
3. Optional: run the included servers (e.g. `uv run github-mcp --transport streamable-http`).
4. Launch the Pocket CLI: `uv run pocket-app`.
5. Launch the GitHub CLI: set `GITHUB_MCP_PAT` in `.env`, then `uv run plog-app`.

Both CLIs connect to their configured servers automatically and provide
the shared command set:

- `/help` - list available commands.
- `/list [server]` - show resources, templates, and tools.
- `/execute <server> <tool>` - call a tool, prompting for arguments.
- `/exit` - close the session.

## Agent mode (optional)

Agent mode is enabled per app by passing an `AgentConfig` into `Mash`.
The runtime uses Anthropic's client for LLM calls and supports tool search,
token usage telemetry, and tracing.

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
                mode="hybrid",
            ),
            **kwargs,
        )
```

To use agent mode, install the Anthropic SDK and configure credentials:
`pip install anthropic` and `ANTHROPIC_API_KEY=...`.

## Module docs

- `src/mash/README.md` - CLI framework + agent runtime details.
- `src/mashnet/README.md` - MCP client/host details.
- `src/apps/README.md` - App layout and how to add new CLIs.

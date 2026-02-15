## apps

`apps` contains example CLIs built on top of `mash`. Each app subclasses
`mash.Mash`, defines its MCP server configs, and registers any app-specific
commands.

### Included apps

- `pocket` (`pocket-agent`) - connects to the hosted Pocket MCP server and enables
  agent mode with an Anthropic-backed runtime.
- `codebase` (`codebase-agent`) - answers questions about local or GitHub
  repositories with agent mode enabled.
- `db` (`db-agent`) - connects to the BigQuery MCP server for data exploration.

### Running the apps

- Pocket: `uv run pocket-agent`
- Codebase: `uv run codebase-agent`
- DB: `uv run db-agent`

### Configuration

Each app should define a `config.py` that loads `.env` once and exposes any
configuration constants used by the CLI (model names, API keys, MCP URLs). Keep
environment loading centralized so the CLI stays lean.

#### Pocket (`src/apps/pocket/config.py`)

- `ANTHROPIC_API_KEY` - required for agent mode.
- `ANTHROPIC_MODEL` - optional; defaults to `claude-haiku-4-5-20251001`.
- Logs: `src/apps/pocket/pocket.log` (JSONL events).

The Pocket CLI seeds `AgentConfig.system_prompt` with guidance about available MCP
tools (`search`, `concierge`, `company_profile`).

#### Codebase (`src/apps/codebase/config.py`)

- `ANTHROPIC_API_KEY` - required for agent mode.
- `ANTHROPIC_MODEL` - optional; defaults to `claude-sonnet-4-5-20250929`.
- `GITHUB_MCP_URL` - optional; defaults to `https://api.githubcopilot.com/mcp/`
- `GITHUB_MCP_PAT` - required for GitHub repository mode.

#### DB (`src/apps/db/config.py`)

- `ANTHROPIC_API_KEY` - required for agent mode.
- `ANTHROPIC_MODEL` - optional; defaults to `claude-haiku-4-5-20251001`.
- `BIGQUERY_MCP_URL` - optional; defaults to `https://bigquery.googleapis.com/mcp`.
- `BIGQUERY_PROJECT_ID` - strongly recommended; used for `x-goog-user-project`.
- `BIGQUERY_ALLOWED_TOOLS` - optional CSV override for the tool allowlist.

### Creating a new app

1. Create a package under `src/apps/<name>/`.
2. Define your server list (name, url, headers as needed).
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

To enable agent mode, pass an `AgentConfig` into `Mash` and ensure the
`anthropic` dependency is installed.

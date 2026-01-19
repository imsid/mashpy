## apps

`apps` contains example CLIs built on top of `mash`. Each app subclasses
`mash.Mash`, defines its MCP server configs, and registers any app-specific
commands.

### Included apps

- `pocket` (`pocket-app`) - connects to the hosted Pocket MCP server and enables
  agent mode with an Anthropic-backed runtime.
- `plog` (`plog-app`) - connects to GitHub's MCP server using a PAT from `.env`.

### Running the apps

- Pocket: `uv run pocket-app`
- GitHub: `uv run plog-app`

### Configuration

#### Pocket (`src/apps/pocket/cli.py`)

- `ANTHROPIC_API_KEY` - required for agent mode.
- `ANTHROPIC_MODEL` - optional; defaults to `claude-haiku-4-5-20251001`.
- Logs: `src/apps/pocket/pocket.log` (JSONL events).

The Pocket CLI seeds `AgentConfig.app_context` with guidance about available MCP
tools (`search`, `concierge`, `company_profile`).

#### GitHub (`src/apps/plog/cli.py`)

- `GITHUB_MCP_PAT` - required (set in `.env`).
- Logs: `src/apps/plog/plog.log` (JSONL events).

Agent mode is not enabled for `plog` by default. You can add it by passing an
`AgentConfig` when subclassing `Mash`.

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

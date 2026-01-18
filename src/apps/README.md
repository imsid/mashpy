## apps

`apps` contains example CLIs built on top of `mash`.

### Included apps

- `pocket` - connects to the hosted Pocket MCP server.
- `plog` - connects to GitHub's MCP server using a PAT from `.env`.

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

To enable agent mode, pass an `AgentConfig` into `Mash` and install the
Anthropic SDK as described in the repo README.

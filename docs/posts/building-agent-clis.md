---
title: Building an Agent CLI
description: Ship a branded terminal client with mash.cli.App.
date: 2026-06-08
author: imsid
tags:
  - guide
  - cli
---

# Building an Agent CLI

A Mash app ships its own client by configuring `App`. It uses the same live
REPL, request streaming, session picker and one-shot commands as `mash`.
Deploy your pool first and define a host with `HostBuilder.host(...)` or
`mash compose --host my-app --agents my-agent,docs-copilot`.

## Define the client

```python
# my_app/cli.py
import getpass
from mash.cli import App, Command


def hello(ctx, args):
    ctx.renderer.info(f"Hello, {ctx.user_id}!")


app = App(
    name="my-app",
    default_host="my-app",
    default_base_url="http://127.0.0.1:8000",
    env_prefix="MY_APP",
    default_user=getpass.getuser,
    commands=[Command(name="hello", help="Say hello", handler=hello, group="App")],
    theme={"accent": "green"},
    tool_lines={"search_docs": lambda args: f"Searching docs for {args.get('query', '')}"},
)


def main() -> int:
    return app.main()
```

Use `default_agent="my-agent"` instead of `default_host` to talk directly to
one agent. The app's target can be overridden with `--host` or `--agent`.
The host and its composition belong to the deployment; opening the client
does not change them.

Register the executable in `pyproject.toml`:

```toml
[project.scripts]
my-app = "my_app.cli:main"
```

Install the project, then run:

```bash
my-app
my-app repl --continue
my-app repl --resume
my-app run "summarize yesterday's errors"
my-app workflow run export --input '{"table": "bills"}'
my-app status
```

## Connection and user

Flags override environment variables. `MY_APP_API_BASE_URL`, `MY_APP_API_KEY`
and `MY_APP_USER_ID` take precedence over the corresponding `MASH_` variables.
The default URL is used next, then the connection saved by `connect`. Saved
credentials and users are used only for the same deployment URL. The user
falls back to `default_user`, which can be a string or a callable.

```bash
my-app connect --user-id sam
my-app repl --user-id sam --agent docs-copilot
```

The app name appears in its header and names its history file under
`~/.mash/history/`. Operator commands are omitted unless the app sets
`operator_commands=True`.

## Extend the REPL

A `Command` handler receives the current `CLIContext` and parsed argument
list. It may be an async function; synchronous handlers run in a worker
thread so input and streaming stay responsive. Use `ctx.renderer` for output.
Commands appear in `/help` and slash completion, grouped by `Command.group`.

`tool_lines` callbacks receive tool arguments and return a short line, used
both while a call runs and when it finishes. `theme` applies to Rich and
prompt_toolkit. `workflow_renderers={"export": render_export}` supplies a
callback for that workflow's result, in the REPL and top-level workflow
commands.

For the complete command and customization reference, see the
[CLI README](../../src/mash/cli/README.md).

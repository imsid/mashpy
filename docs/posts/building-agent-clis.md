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
Deploy your pool first, define a host with `HostBuilder.host(...)` or
`mash compose --host my-app --agents my-agent,docs-copilot`, and create the
policy its events run under with `mash policy put my-app --file policy.json`.

## Define the client

```python
# my_app/cli.py
import getpass
from mash.cli import App, Command


def hello(ctx, args):
    ctx.renderer.info(f"Hello from {ctx.app_id}!")


app = App(
    name="my-app",
    default_host="my-app",
    default_base_url="http://127.0.0.1:8000",
    env_prefix="MY_APP",
    default_app="my-app",
    default_policy=lambda: f"my-app-{getpass.getuser()}",
    commands=[Command(name="hello", help="Say hello", handler=hello, group="App")],
    theme={"accent": "green"},
    tool_lines={"search_docs": lambda args: f"Searching docs for {args.get('query', '')}"},
)


def main() -> int:
    return app.main()
```

Every message is an event posted to the host, and every event names an app
and a policy. Here each operating-system user gets a policy of their own, so
each has their own sandbox and browser profile. `--host` overrides the app's
target. The host and its composition belong to the deployment; opening the
client does not change them.

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

## Connection, app and policy

Flags override environment variables. `MY_APP_API_BASE_URL`, `MY_APP_API_KEY`,
`MY_APP_APP_ID` and `MY_APP_POLICY_ID` take precedence over the corresponding
`MASH_` variables. The default URL is used next, then the connection saved by
`connect`. A saved key, app and policy are used only for the same deployment
URL. The app and policy fall back to `default_app` and `default_policy`, each
a string or a callable. The policy must already exist on the deployment.

```bash
my-app connect --app-id my-app --policy-id my-app-sam
my-app repl --policy-id my-app-sam --host my-app
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

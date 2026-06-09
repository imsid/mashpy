---
title: Building an Agent CLI with Mash
description: Build a custom CLI for your Mash agent — serve it, connect as a remote shell, and add your own REPL commands.
date: 2026-06-08
author: imsid
tags:
  - guide
  - cli
---

# Building an Agent CLI with Mash

This guide walks through building a custom CLI for your Mash agent. You
define the agent with an `AgentSpec`, serve it with `mash host serve`, then
wire up a dedicated CLI that connects as a remote shell with built-in
commands and your own custom REPL commands.

## Overview

A Mash agent CLI has three layers:

1. **Agent definition** — an `AgentSpec` subclass plus a `build_host()`
   function that composes agents into an `AgentHost`.
2. **Host server** — `mash host serve` (or programmatic `run_host`) exposes
   the agent over HTTP.
3. **CLI client** — a Python entrypoint that creates a `MashRemoteShell`,
   registers custom commands, and runs the interactive REPL.

```
┌──────────────┐         HTTP/SSE          ┌──────────────┐
│  Your CLI    │ ◄─────────────────────►   │  Agent Host  │
│  (shell.py)  │    MashHostClient         │  (spec.py)   │
└──────────────┘                           └──────────────┘
```

## Step 1: Define Your Agent

Create `my_agent/spec.py` with your `AgentSpec` and a `build_host` function:

```python
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentSpec, HostBuilder
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry
from mash.tools.bash import BashTool


class MyAgentSpec(AgentSpec):
    def get_agent_id(self) -> str:
        return "my-agent"

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir="/path/to/workspace"))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="my-agent")

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id="my-agent",
            system_prompt="You are a helpful coding assistant.",
        )


def build_host():
    return HostBuilder().primary(MyAgentSpec()).build()
```

Start the host:

```bash
mash host serve --host-app my_agent.spec:build_host --port 8000
```

At this point you can already talk to your agent with the generic Mash CLI:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --agent my-agent
```

The next step is building a dedicated CLI with custom commands tailored to
your agent's domain.

## Step 2: Build Your CLI

Create `my_agent/cli.py`. The key imports from `mash.cli`:

| Import | Purpose |
|---|---|
| `MashHostClient` | HTTP client that talks to the host API |
| `MashRemoteShell` | Interactive shell with REPL, rendering, and command dispatch |
| `ShellTarget` | Connection target (URL, agent id, session id) |
| `Command` | Defines a slash command (name, help text, handler) |
| `CLIContext` | Passed to every command handler (client, renderer, session state) |

Here is a minimal CLI entrypoint:

```python
import argparse
import os

from mash.cli import MashHostClient, MashRemoteShell, ShellTarget

DEFAULT_BASE_URL = os.environ.get("MY_AGENT_API_URL", "http://127.0.0.1:8000")


def main():
    parser = argparse.ArgumentParser(prog="my-agent")
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--agent", default=None)
    args = parser.parse_args()

    base_url = args.api_base_url
    client = MashHostClient(base_url, api_key=args.api_key)

    try:
        # Auto-resolve the primary agent if not specified
        if args.agent:
            agent_id = args.agent
        else:
            health = client.health()
            agent_id = health["deployment"]["primary_agent_id"]

        target = ShellTarget(
            api_base_url=base_url,
            agent_id=agent_id,
            session_id=MashRemoteShell.new_session_id(),
        )
        shell = MashRemoteShell(client, target)

        # Register custom commands here (see Step 3)

        shell.run()
    finally:
        client.close()
```

`shell.run()` starts the interactive REPL. The user types messages that go
to the agent, and slash commands that execute locally. Every shell comes
with these built-in commands out of the box:

| Command | What it does |
|---|---|
| `/help` | List all registered commands |
| `/status` | Show deployment and connection info |
| `/agents` | List available agents |
| `/use <id>` | Switch to a different agent (primary or subagent) |
| `/session` | Show current session info (model, tokens, etc.) |
| `/sessions` | List all sessions for the current agent |
| `/history [N]` | View conversation history |
| `/workflow list\|run\|status` | List, run, and inspect workflows |
| `/clear` | Clear the screen |
| `/exit` | Exit the REPL |

## Step 3: Add Custom Commands

Custom commands are where your CLI becomes domain-specific. Each command
is a `Command` with a name, help text, and a handler function that receives
`CLIContext` and a list of string arguments.

```python
from mash.cli import Command


def register_my_commands(shell):
    def deploy_command(ctx, args):
        if not args:
            ctx.renderer.error("Usage: /deploy <environment>")
            return
        env = args[0]
        ctx.renderer.info(f"Deploying to {env}...")

        # Use the agent to generate a deploy plan
        request_id = ctx.client.submit_request(
            ctx.agent_id,
            message=f"Generate a deploy checklist for {env}",
            session_id=ctx.session_id,
        )
        for event in ctx.client.stream_request(ctx.agent_id, request_id):
            event_name = event.get("event")
            payload = event.get("data") or {}

            if event_name == "agent.trace":
                shell.render_runtime_trace_payload(payload)
                continue
            if event_name == "request.completed":
                response = (payload.get("response") or {}).get("text", "")
                if response:
                    ctx.renderer.markdown(response)
                break

    shell.register_command(
        Command(
            name="deploy",
            help="Generate a deploy checklist for an environment",
            handler=deploy_command,
        )
    )
```

Then register it in your CLI entrypoint before `shell.run()`:

```python
shell = MashRemoteShell(client, target)
register_my_commands(shell)
shell.run()
```

### CLIContext reference

Every command handler receives `ctx: CLIContext` with these attributes:

| Attribute | Type | Description |
|---|---|---|
| `ctx.client` | `MashHostClient` | HTTP client for the host API |
| `ctx.renderer` | `RichRenderer` | Terminal output (info, error, markdown, tables) |
| `ctx.agent_id` | `str` | Currently active agent id |
| `ctx.session_id` | `str` | Current session id |
| `ctx.api_base_url` | `str` | Host base URL |
| `ctx.session_ids` | `dict` | Map of agent id → session id for multi-agent switching |

### Renderer methods

`ctx.renderer` provides these output methods:

```python
ctx.renderer.info("Status message")
ctx.renderer.warn("Warning message")
ctx.renderer.error("Error message")
ctx.renderer.markdown("**Rich** markdown output")
ctx.renderer.print("Plain text")
ctx.renderer.table(["Col A", "Col B"], [["row1a", "row1b"], ["row2a", "row2b"]])
ctx.renderer.clear()
```

## Step 4: Wire Up Workflow Commands

If your agent host has registered workflows, the built-in `/workflow` command
handles listing, running, and checking status. But you can also create
dedicated commands for specific workflows that provide a better UX.

Here is the pattern used by the Mash Pilot project for a changelog workflow:

```python
from mash.cli import Command


def register_changelog_command(shell):
    WORKFLOW_ID = "my-changelog"

    def changelog_command(ctx, args):
        commit_count = 5
        if args:
            commit_count = int(args[0])

        # Start the workflow
        run = ctx.client.run_workflow(
            WORKFLOW_ID,
            workflow_input={"commit_count": commit_count},
        )
        run_id = run.get("run_id")
        ctx.renderer.info(f"Workflow started: {run_id}")

        # Stream workflow events with real-time trace rendering
        try:
            for event in ctx.client.stream_workflow_run(WORKFLOW_ID, run_id):
                event_name = event.get("event")
                payload = event.get("data") or {}
                task_agent_id = payload.get("task_agent_id") or ""

                if event_name == "agent.trace":
                    shell.render_runtime_trace_payload(
                        payload,
                        trace_label="Changelog",
                        agent_id=task_agent_id or None,
                    )
                    continue

                if event_name == "request.completed":
                    response = (payload.get("response") or {}).get("text", "")
                    ctx.renderer.markdown(response)
                    break
        finally:
            shell.chain_renderer.finish_trace()

    shell.register_command(
        Command(
            name="changelog",
            help="Generate a changelog from recent commits",
            handler=changelog_command,
        )
    )
```

Key points:

- `ctx.client.run_workflow()` starts a workflow run and returns its id.
- `ctx.client.stream_workflow_run()` yields SSE events as the workflow
  executes.
- `shell.render_runtime_trace_payload()` renders the agent's chain of
  thought in real time (steps, tool calls, token counts, durations).
- Always call `shell.chain_renderer.finish_trace()` in a `finally` block
  to clean up the trace renderer.

## Step 5: Package as a Console Script

Add your CLI to `pyproject.toml` so it installs as a shell command:

```toml
[project.scripts]
my-agent = "my_agent.cli:main"
```

After `pip install -e .`, users can run:

```bash
my-agent --api-base-url http://localhost:8000
```

## Full Example: Pilot CLI

The [Mash Pilot](https://github.com/imsid/mash-pilot) project is a
real-world example that follows this pattern. Its structure:

```
pilot/
  spec.py       # PilotSpec + copilot subagents + build_host()
  cli.py        # CLI entrypoint with MashRemoteShell
  tools.py      # Custom tools (UpdateDocsTool)
  prompt.py     # System prompt construction
  workflows/
    changelog.py  # /changelog command + workflow definition
    quiz.py       # /quiz command + QuizAgentSpec + workflow definition
  skills/
    changelog/SKILL.md
    mash-quiz/SKILL.md
```

The CLI entrypoint (`pilot/cli.py`) is ~95 lines:

1. Parse args and resolve the connection
2. Create `MashHostClient` and `MashRemoteShell`
3. Register custom commands (`/changelog`, `/quiz`)
4. Call `shell.run()`

The workflow commands (`/changelog`, `/quiz`) demonstrate two patterns:

- **Dynamic workflows** — `/changelog` registers a skill and workflow
  definition at runtime, then runs it. Good for workflows that need
  runtime configuration.
- **Static workflows** — `/quiz` relies on a workflow pre-registered during
  `build_host()`. The command just triggers a run and streams the output.
  Good for workflows that are always available.

## Summary

| What you write | What it does |
|---|---|
| `AgentSpec` subclass | Defines your agent's tools, skills, LLM, and system prompt |
| `build_host()` function | Composes agents and workflows into an `AgentHost` |
| CLI entrypoint | Creates `MashRemoteShell`, registers commands, calls `shell.run()` |
| Custom `Command` handlers | Domain-specific slash commands using `CLIContext` |
| Workflow commands | Start workflows and stream real-time execution traces |

The `mash.cli` module handles connection management, the REPL loop,
command dispatch, session routing across agents, chain-of-thought
rendering, and all the built-in commands. Your CLI code only needs to
define the commands that are specific to your agent's domain.

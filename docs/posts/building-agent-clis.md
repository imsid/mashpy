---
title: Building an Agent CLI
description: Build your own CLI for your Mash agents. Wrap the remote shell, connect and compose through it, and add your own REPL commands.
date: 2026-06-08
author: imsid
tags:
  - guide
  - cli
---

# Building an Agent CLI

This guide walks through building your own CLI, `my-cli`, for your Mash
agents. You define agents with `AgentSpec` subclasses, deploy them as a pool
with `mash host serve`, wrap the Mash remote shell in your own entrypoint,
then teach it to compose a host and carry custom REPL commands.

## Overview

A Mash agent CLI has four layers:

1. **Agent definitions**: `AgentSpec` subclasses plus a `build_pool()`
   function that registers them into an `AgentPool`.
2. **Host server**: `mash host serve` (or programmatic `run_host`) exposes
   the pool over HTTP.
3. **CLI client**: a Python entrypoint that creates a `MashRemoteShell` and
   runs the interactive REPL. This is `my-cli`.
4. **Host composition**: a `Host` naming one agent as primary and others as
   subagents, which `my-cli` defines on startup.

```
┌──────────────┐         HTTP/SSE          ┌─────────────────────┐
│  my-cli      │ ◄─────────────────────►   │  Agent Pool         │
│  (cli.py)    │    MashHostClient         │  (spec.py)          │
└──────────────┘                           │  hosts: assistant…  │
                                           └─────────────────────┘
```

The pool is the unit of deploy. A host is the unit of composition: it picks
a primary and a set of subagents from the pool, and the primary gets an
`InvokeSubagent` tool plus a routing directory for exactly that set, per
request. The same pool can serve several hosts at once.

## Step 1: Define the Pool

Create `my_agent/spec.py`. Every agent registers with `AgentMetadata`, the
self-description that delegation decisions are made from:

```python
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentMetadata, AgentSpec, HostBuilder
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


class DocsCopilotSpec(AgentSpec):
    # same shape: get_agent_id() -> "docs-copilot", its own tools and prompt
    ...


def build_pool():
    return (
        HostBuilder()
        .agent(
            MyAgentSpec(),
            metadata=AgentMetadata(
                display_name="My Agent",
                description="Coding assistant for my workspace.",
                capabilities=["bash"],
                usage_guidance="Default agent for user requests.",
            ),
        )
        .agent(
            DocsCopilotSpec(),
            metadata=AgentMetadata(
                display_name="Docs Copilot",
                description="Answers questions about the project docs.",
                capabilities=["documentation"],
                usage_guidance="Use for documentation questions.",
            ),
        )
        .build()
    )
```

No agent is registered as a primary or a subagent. Roles come from a host
composition, which `my-cli` will define in Step 3. (A `.host(...)` call on
the builder can also ship a composition with the deploy; this guide defines
it from the CLI instead.)

Start the server:

```bash
mash host serve --host-app my_agent.spec:build_pool --port 8000
```

## Step 2: Wrap the Shell as `my-cli`

Create `my_agent/cli.py`. The minimum viable CLI is a `MashHostClient`
pointed at the deployment, a `ShellTarget` naming an agent, and a
`MashRemoteShell` running the REPL:

```python
import argparse
import os

from mash.cli import MashHostClient, MashRemoteShell, ShellTarget

DEFAULT_BASE_URL = os.environ.get("MY_CLI_API_URL", "http://127.0.0.1:8000")


def main():
    parser = argparse.ArgumentParser(prog="my-cli")
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=os.environ.get("MASH_API_KEY"))
    args = parser.parse_args()

    client = MashHostClient(args.api_base_url, api_key=args.api_key)
    try:
        agents = client.list_agents()
        target = ShellTarget(
            api_base_url=args.api_base_url,
            agent_id=agents[0]["agent_id"],
            session_id=MashRemoteShell.new_session_id(),
        )
        MashRemoteShell(client, target).run()
    finally:
        client.close()


if __name__ == "__main__":
    main()
```

Run it:

```bash
python -m my_agent.cli
```

This already gives you a working terminal client against the first agent in
the pool: messages go to the agent, responses stream back with live
chain-of-thought rendering, and the shell ships these built-in commands:

| Command | What it does |
|---|---|
| `/help` | List all registered commands |
| `/status` | Show deployment, current host, agent, and session |
| `/agents` | List pooled agents |
| `/hosts` | List defined host compositions |
| `/session` | Show current session info (model, tokens, etc.) |
| `/sessions` | List all sessions for the current agent |
| `/history [N]` | View conversation history |
| `/workflow list\|run\|status` | List, run, and inspect workflows |
| `/trace [N]` | Show timing analysis for recent traces |
| `/clear` | Clear the screen |
| `/exit` | Exit the REPL |

The imports this guide builds on, all from `mash.cli`:

| Import | Purpose |
|---|---|
| `MashHostClient` | HTTP client that talks to the host API |
| `MashRemoteShell` | Interactive shell with REPL, rendering, and command dispatch |
| `ShellTarget` | Connection target (URL, agent id, session id, optional host id) |
| `Command` | Defines a slash command (name, help text, handler) |
| `CLIContext` | Passed to every command handler (client, renderer, session state) |

What's missing from the minimal version is composition: the agent runs
bare, with no subagents. That's the next step.

## Step 3: Connect and Compose

`my-cli` owns its composition. On startup it defines a host over the pool
and pins the shell to it, so every message routes through the primary with
the right subagents wired in:

```python
import argparse
import os

from mash.cli import MashHostClient, MashRemoteShell, ShellTarget

DEFAULT_BASE_URL = os.environ.get("MY_CLI_API_URL", "http://127.0.0.1:8000")
HOST_ID = "assistant"


def main():
    parser = argparse.ArgumentParser(prog="my-cli")
    parser.add_argument("--api-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=os.environ.get("MASH_API_KEY"))
    parser.add_argument("--agent", default=None, help="bare-agent mode, skips the host")
    args = parser.parse_args()

    client = MashHostClient(args.api_base_url, api_key=args.api_key)
    try:
        if args.agent:
            # Bare agent: no composition, no delegation
            agent_id, host_id = args.agent, None
        else:
            # Define (or refresh) the CLI's composition, then target it.
            # The PUT is idempotent, so running this on every startup is fine
            # and also covers server restarts.
            described = client.define_host(
                HOST_ID,
                primary="my-agent",
                subagents=["docs-copilot"],
            )
            agent_id = described["primary"]["agent_id"]
            host_id = HOST_ID

        target = ShellTarget(
            api_base_url=args.api_base_url,
            agent_id=agent_id,
            session_id=MashRemoteShell.new_session_id(),
            host_id=host_id,
        )
        shell = MashRemoteShell(client, target)

        # Register custom commands here (see Step 4)

        shell.run()
    finally:
        client.close()
```

`define_host` issues `PUT /v1/hosts/assistant` to the deployment. If the
composition references an agent that isn't in the pool, the server rejects
it with a clear error before any shell starts. Hosts are in-memory on the
server, so defining on every startup also covers deployment restarts.

When `ShellTarget.host_id` is set, the shell submits every REPL message via
`POST /v1/hosts/{host_id}/request` and streams the response from the primary
agent the server names in its reply. When it's `None`, messages go straight
to `POST /v1/agent/{agent_id}/request` and the agent runs without subagents.

The target is fixed for the shell's lifetime; there is no command for
switching it inside the REPL. A CLI that offers several teams exposes them
as entrypoint flags and starts the shell with the chosen one:

```bash
my-cli                          # the assistant composition
my-cli --agent docs-copilot     # one agent, no delegation
```

The stock `mash` CLI drives the same flow generically (`mash connect`,
`mash compose`, `mash agents`, `mash hosts`, `mash repl`), which is handy
for poking at a deployment before your CLI exists.

## Step 4: Add Custom Commands

Custom commands are where your CLI becomes domain-specific. Each command
is a `Command` with a name, help text, and a handler function that receives
`CLIContext` and a list of string arguments.

When a command sends a message to the agent, it should respect the shell's
target: route through the host when one is set, so the request gets the
composition's subagents.

```python
from mash.cli import Command


def submit_through_target(ctx, message):
    """Submit a message via the shell's host when set, else the bare agent.

    Returns (request_id, agent_id_to_stream_from).
    """
    if ctx.host_id:
        accepted = ctx.client.submit_host_request(
            ctx.host_id,
            message=message,
            session_id=ctx.session_id,
        )
        return accepted["request_id"], accepted["agent_id"]
    request_id = ctx.client.submit_request(
        ctx.agent_id,
        message=message,
        session_id=ctx.session_id,
    )
    return request_id, ctx.agent_id


def register_my_commands(shell):
    def deploy_command(ctx, args):
        if not args:
            ctx.renderer.error("Usage: /deploy <environment>")
            return
        env = args[0]
        ctx.renderer.info(f"Deploying to {env}...")

        request_id, stream_agent_id = submit_through_target(
            ctx, f"Generate a deploy checklist for {env}"
        )
        for event in ctx.client.stream_request(stream_agent_id, request_id):
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
| `ctx.agent_id` | `str` | The target agent (the host's primary when a host is set) |
| `ctx.host_id` | `str \| None` | The host composition the shell is pinned to, if any |
| `ctx.session_id` | `str` | Current session id |
| `ctx.api_base_url` | `str` | Host base URL |
| `ctx.session_ids` | `dict` | Map of agent id to session id |

### Host-related client methods

`MashHostClient` covers the composition surface, so custom commands and
entrypoints never need raw HTTP:

```python
client.list_hosts()                                  # GET  /v1/hosts
client.get_host("assistant")                         # GET  /v1/hosts/assistant
client.define_host("assistant",                      # PUT  /v1/hosts/assistant
                   primary="my-agent",
                   subagents=["docs-copilot"],
                   workflows=[])
client.submit_host_request("assistant",              # POST /v1/hosts/assistant/request
                           message="...",
                           session_id="s-1")
```

`submit_host_request` returns `{"request_id", "agent_id", "session_id"}`.
Stream from the returned `agent_id` with the same `stream_request` used for
bare submissions.

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

## Step 5: Wire Up Workflow Commands

If your pool has registered workflows, the built-in `/workflow` command
handles listing, running, and checking status. But you can also create
dedicated commands for specific workflows that provide a better UX.

Workflow runs target a registered agent directly and don't involve host
composition, so this pattern is the same regardless of how the shell is
targeted. Here is the pattern used by the Mash Pilot project for a
changelog workflow:

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

## Step 6: Package as a Console Script

Add your CLI to `pyproject.toml` so it installs as a shell command:

```toml
[project.scripts]
my-cli = "my_agent.cli:main"
```

After `pip install -e .`, users can run:

```bash
my-cli --api-base-url http://localhost:8000
```

## Full Example: Pilot CLI

The [Mash Pilot](https://github.com/imsid/mash-pilot) project is a
real-world example that follows this pattern. Its structure:

```
pilot/
  spec.py       # PilotSpec + copilot specs + build_pool()
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

`spec.py` registers the pilot agent and its five copilots into the pool.
The CLI entrypoint (`pilot/cli.py`):

1. Parse args and resolve the connection
2. Create `MashHostClient`, define the `pilot` host (primary `pilot`, the
   copilots as subagents; `define_host` is idempotent), and build a
   `ShellTarget` with `host_id="pilot"`
3. Create the `MashRemoteShell` and register custom commands
   (`/changelog`, `/quiz`)
4. Call `shell.run()`

The workflow commands (`/changelog`, `/quiz`) demonstrate two patterns:

- **Dynamic workflows**: `/changelog` registers a skill and workflow
  definition at runtime, then runs it. Good for workflows that need
  runtime configuration.
- **Static workflows**: `/quiz` relies on a workflow pre-registered during
  `build_pool()`. The command just triggers a run and streams the output.
  Good for workflows that are always available.

## Summary

| What you write | What it does |
|---|---|
| `AgentSpec` subclasses | Define each agent's tools, skills, LLM, and system prompt |
| `build_pool()` function | Registers agents and workflows into an `AgentPool` |
| CLI entrypoint | Defines the host composition, creates `MashRemoteShell` pinned to it, registers commands |
| Custom `Command` handlers | Domain-specific slash commands using `CLIContext`, routed through the shell's target |
| Workflow commands | Start workflows and stream real-time execution traces |

The `mash.cli` module handles connection management, the REPL loop,
command dispatch, host-routed submission, chain-of-thought rendering, and
all the built-in commands. Your CLI code only needs to define the
composition and the commands that are specific to your agents' domain.

For the same composition flow driven purely over HTTP, without the CLI,
see [Building Dynamic Hosts over the API](building-dynamic-hosts-apis.md).

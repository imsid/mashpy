# Mash Product Brief

Mash is a self-hosted runtime for multi-agent applications. It is built around
[Host-to-Agent Protocol (H2A)](docs/rfcs/host-to-agent-protocol.md) -- protocol boundary for submitting requests, streaming
events, and running hosted agents.

## What Mash Provides

- [Agent Specs governed by a single Host](#agent-specs-governed-by-a-single-host)
- [Durable Harness](#durable-harness)
    - [Core Agent Loop: Context, Memory, Tools, Skills, Signals, Structured output](#core-agent-loop-context-memory-tools-skills-signals-structured-output)
    - [Human-in-the-Loop Interactions](#human-in-the-loop-interactions)
    - [Workflows](#workflows)
- [Observability Built Into the Runtime](#observability-built-into-the-runtime)
- [Built-In Eval and Trace Digest](#built-in-eval-and-trace-digest)
- [Self-Hosted Interfaces for Use and Operations](#self-hosted-interfaces-for-use-and-operations)
    - [CLI Commands](#cli-commands)

### Agent Specs governed by a single Host

Mash lets teams define a primary agent, add specialized subagents, and compose
workflow-only agents behind the same host. That makes it possible to build
systems where general-purpose agents delegate to focused specialists without
introducing a separate coordination layer outside the runtime.

```python
host = (
    HostBuilder()
    .primary(PrimaryAgent())
    .subagent(CliCopilot(),  metadata={"handles": "CLI questions"})
    .subagent(ApiCopilot(),  metadata={"handles": "API questions"})
    .build()
)
```

### Durable Harness

Mash provides the harness to durably execute a request — accepted, executed
through a durable request engine, and recorded as replayable runtime events. That gives teams a stronger operational model than a
single in-memory agent loop and makes retries, restarts, and long-running work
much easier to manage.

#### Core Agent Loop: Context, Memory, Tools, Skills, Signals, Structured output

This gives teams a practical way to move beyond text-in, text-out
behavior and build agents that can act with context, preserve useful state, and
expose structured outputs from each completed loop which makes it easier to build typed
integrations, machine-readable agent responses, and predictable downstream
automation.

Mash ships with runtime tools that are available to every agent out of the box:

- **BashTool** — execute shell commands in the host environment
- **AskUserTool** — pause execution to ask the user a question (durable)
- **search_conversations** — search stored conversation turns and return ranked previews
- **get_full_turn_message** — expand search results into full turn text

```
                  ┌─────────────────────────────────────────┐
                  │          Durable Request                │
                  │                                         │
                  │   ┌─ context ─── memory ──┐             │
                  │   │                       │             │
request ────────► │   │     Agent Loop        │ ──► signals │
(cli/api)         │   │ think → act → observe │      │      │
                  │   │                       │      ▼      │
                  │   └─ tools ───── skills ──┘  structured │
workflow ───────► │        ▲                      output    │
(schedule/trigger)│        │ user interaction               │
                  │        ▼ (approval / ask-user)          │
                  │                                         │
                  │       resumable · replayable            │
                  └─────────────────────────────────────────┘
```

#### Human-in-the-Loop Interactions

Mash supports durable agent-to-user interactions as part of the hosted runtime.
An agent can pause mid-execution to request approval before a sensitive tool
runs, or ask the user a question when it needs information to continue. These
interactions are durable: the runtime can restart, and the waiting agent resumes
exactly where it left off when the user responds — whether that takes seconds or
hours.

Tool developers gate execution behind user consent by setting a single attribute
on the tool definition. Agents ask users questions by calling a built-in tool
that the runtime intercepts and translates into a durable interaction. Both paths
use the same protocol-level interaction events and the same host-to-client
response flow, so the operational model stays simple regardless of who initiates
the interaction.

```python
# tool gated behind user consent
class DeployTool:
    name = "deploy_service"
    requires_approval = True
```

```python
# agent asks the user mid-execution
result = await ask_user(
    question="Which database should we use?",
    options=["PostgreSQL", "MongoDB", "SQLite"]
)
```

#### Workflows

Mash workflows are ordered sequences of tasks. Each task runs through a
registered agent and is paired with a skill and a structured
output. That structured output becomes the task's persisted state for future
runs and downstream steps.

Mash supports both code-defined workflows and dynamic workflow and skill
registration, so teams can define workflows in host code or publish them at
runtime. This makes workflows a concrete execution model for repeatable,
stateful agent tasks rather than a loose orchestration layer.


### Observability Built Into the Runtime

Mash includes runtime-level telemetry and replayable runtime events so teams can
inspect how a request moved through the system, not just what the final answer
was. The telemetry UI gives operators a request and trace view, and memory
search is available alongside that runtime observability so teams can inspect
both execution behavior and stored conversational context. This matters when
agents become operational software and developers need traceability, debugging,
and a clear view of runtime behavior.

```
GET /api/v1/telemetry/events           # runtime events
GET /api/v1/telemetry/events/stream    # tail events (SSE)
GET /api/v1/telemetry/memory/search    # search agent memory

Telemetry UI → http://<MASH_HOST_URL>/telemetry
```

### Built-In Eval and Trace Digest

Mash includes Masher, a built-in agent that exposes workflows to:
- summarize a trace into a digest that captures the key request, execution flow, and outcome
- generate a normalized online eval record from that trace for downstream analysis and curation

This gives teams an immediate built-in path for runtime analysis and eval
generation without first building a separate post-processing pipeline.

```
/workflow run masher-trace-digest --input '{"mode": "trace", "target_agent_id": "..", "session_id": "..", "trace_id": ".."}'

/workflow run masher-online-eval-curation --input '{"mode": "incremental", "target_agent_id": ".."}'
```

### Self-Hosted Interfaces for Use and Operations

Mash exposes agents over HTTP, supports streaming responses, includes a CLI and
REPL, and fits naturally into local, server, and containerized deployments. The
same platform can be used to develop an agent system, integrate it into product
surfaces, and operate it in a controlled environment. Checkout [Crew Agent](https://github.com/imsid/crew/blob/main/docs/product.md) for an implementation.

```bash
mash host serve --host-app pilot.spec:build_host --port 8001
mash connect --api-base-url <MASH_HOST_URL> --api-key secret
```

### REPL
REPL gives you the interface to interact with the agents

```bash
mash repl
```

Example messages inside the REPL:
```text
> Summarize our most recent conversation
> What tools do you have access to?
> How many tokens have I used in this session
> ...
```

### CLI Commands
Default CLI commands exposed by mash
```
/status     Show deployment status
/agents     List available agents
/sessions   List remote sessions
/session    Show current session info
/history    View conversation history
/use        Switch to a different agent
/workflow   List, run, and inspect workflows
```

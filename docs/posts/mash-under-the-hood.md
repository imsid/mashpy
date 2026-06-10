---
title: Mash Under the Hood
description: What Mash provides — one host over many agents, a durable harness, observability, and self-hosted interfaces.
date: 2026-06-10
author: imsid
tags:
  - product
  - overview
---

# Mash Under the Hood

The [product brief](product-brief.md) makes the pitch: applications should
integrate with agents through one standardized seam, and the host — not the
agent — is the unit of deploy. This post covers both halves in practice: the
seam — the host and its interfaces — and the runtime underneath it — the
durable harness and observability — that Mash ships so you never build it
yourself.

- [One Host, Many Agents](#one-host-many-agents)
- [Durable Harness](#durable-harness)
    - [Core Agent Loop: Context, Memory, Tools, Skills, Signals, Structured output](#core-agent-loop-context-memory-tools-skills-signals-structured-output)
    - [Human-in-the-Loop Interactions](#human-in-the-loop-interactions)
    - [Workflows](#workflows)
- [Observability](#observability)
    - [Spans and Trace Analysis](#spans-and-trace-analysis)
    - [Telemetry API](#telemetry-api)
    - [Telemetry UI](#telemetry-ui)
    - [CLI Trace Inspection](#cli-trace-inspection)
    - [Built-In Eval and Trace Digest](#built-in-eval-and-trace-digest)
- [Self-Hosted Interfaces](#self-hosted-interfaces)
    - [REPL](#repl)
    - [CLI Commands](#cli-commands)

## One Host, Many Agents

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

## Durable Harness

Mash provides the harness to durably execute a request — accepted, executed
through a durable request engine, and recorded as replayable runtime events. That gives teams a stronger operational model than a
single in-memory agent loop and makes retries, restarts, and long-running work
much easier to manage.

### Core Agent Loop: Context, Memory, Tools, Skills, Signals, Structured output

This gives teams a practical way to move beyond text-in, text-out
behavior and build agents that can act with context, preserve useful state, and
expose structured outputs from each completed loop which makes it easier to build typed
integrations, machine-readable agent responses, and predictable downstream
automation.

Each agent brings its own `LLMProvider`, so different agents in the same host
can run on different models — a cheap model for triage, a capable one for
complex reasoning. Mash ships providers for Anthropic, Google Gemini, and OpenAI
out of the box. Each provider handles prompt caching differently — Anthropic uses
`cache_control` breakpoints on system and tool blocks, OpenAI uses explicit cache
keys with configurable retention, and Gemini creates server-side cached content
objects with a TTL. Mash abstracts all of this behind a single
`prompt_caching_enabled` flag in `AgentConfig` (on by default). The runtime
automatically applies the right caching strategy for whichever provider the agent
uses, so repeated requests within a session avoid re-processing static context
without requiring any provider-specific code from the developer.

Long-running sessions accumulate token cost as conversation history grows. Mash
handles this with automatic conversation compaction: when a session's total token
count crosses a configurable threshold, the runtime summarizes earlier turns into
a compact summary checkpoint using the agent's own LLM. Future requests load the
summary instead of the full history, keeping context size bounded without losing
key decisions, constraints, or user preferences. Compaction is configurable per
agent via `compaction_token_threshold` and `compaction_turn_limit` in
`AgentConfig`, and is disabled by default (threshold of 0).

Mash ships with runtime tools that are available to every agent out of the box:

- **BashTool** — execute shell commands in the host environment
- **AskUserTool** — pause execution to ask the user a question
- **InvokeSubagent** — delegate work to a registered subagent and stream its response back
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

### Human-in-the-Loop Interactions

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

### Workflows

Mash workflows are ordered sequences of tasks. Each task runs through a
registered agent and is paired with a skill and a structured
output. That structured output becomes the task's persisted state for future
runs and downstream steps.

Mash supports both code-defined workflows and dynamic workflow and skill
registration, so teams can define workflows in host code or publish them at
runtime. This makes workflows a concrete execution model for repeatable,
stateful agent tasks rather than a loose orchestration layer.


## Observability

When agents run as operational software, "it produced the right answer" is not
enough. Teams need to know *where* time was spent, *which* tool call was
slowest, whether cold start dominated latency, and what happened inside a
subagent delegation — without wiring up a third-party APM or building a
post-processing pipeline first. Mash ships observability as a first-class layer
across the runtime, API, UI, and CLI so that every trace is inspectable the
moment it completes.

### Spans and Trace Analysis

Every request produces an ordered stream of `RuntimeEvent` records. Mash
transforms those flat events into a hierarchical **span tree** and computes a deterministic
**trace analysis** from that tree. No LLM inference is involved; all metrics
are derived directly from event timestamps and payloads.

The span tree models the full request lifecycle:

```
TRACE
├── COLD_START        (request accepted → trace started)
├── CONTEXT_LOAD      (trace started → context loaded)
├── STEP 0
│   ├── THINK         (LLM inference, with token usage)
│   ├── TOOL_CALL     (bash, 200ms)
│   └── TOOL_CALL     (read_file, 150ms)
├── STEP 1
│   ├── THINK
│   └── SUBAGENT_CALL (research agent, 3.2s)
└── STEP 2
    └── THINK         (final response)
```

The trace analysis computed from this tree includes:

- **Timing breakdown**: total duration, cold start, context load, LLM think,
  tool execution, subagent calls, and idle time — each with absolute and
  percentage values
- **Per-tool stats**: call count, total/avg/max/min latency, error count —
  sorted by total time so the most expensive tool surfaces first
- **Per-step breakdown**: think, tool, and overhead time for each agent loop
  iteration
- **Slowest operations**: top 10 spans ranked by duration
- **Subagent trace stitching**: child agent traces are linked and analyzed
  recursively up to 3 levels deep

### Telemetry API

The telemetry API exposes both raw events and structured analysis:

```
GET /telemetry/events                  # flat runtime events
GET /telemetry/events/stream           # tail events (SSE)
GET /telemetry/traces                  # list recent traces (lightweight)
GET /telemetry/trace/analysis          # span tree + latency analysis
GET /telemetry/api/events              # backend API request logs
GET /telemetry/memory/search           # search agent memory
```

The `/telemetry/trace/analysis` endpoint returns the full span tree and
analysis dict in a single call — no client-side computation needed.

### Telemetry UI

The built-in telemetry dashboard at `http://<HOST>/telemetry` renders traces
with a visual span waterfall — each span is a collapsible row showing kind,
name, duration, and a proportional bar scaled to the trace total. A summary
bar at the top shows the time distribution across think, tool, subagent, cold
start, and idle phases. Below the waterfall, collapsible panels show tool stats,
step breakdown, and slowest operations tables.

### CLI Trace Inspection

The `/trace` REPL command gives developers instant access to trace analysis
without leaving the terminal:

```
/trace        # analyze the most recent trace
/trace 5      # analyze the 5 most recent traces
```

Each trace renders a summary line (status, duration, steps, tool calls, tokens),
a timing breakdown table with visual bars, per-tool stats, and the top slowest
operations.

### Built-In Eval and Trace Digest

Masher, Mash's built-in workflow agent, exposes two workflows that build on the
same span and analysis infrastructure:

- **`masher-trace-digest`** produces a schema v2 digest with the full latency
  breakdown, tool stats, step breakdown, slowest operations, nested subagent
  traces, and notable events — a complete diagnostic snapshot of one trace.
- **`masher-online-eval-curation`** writes normalized eval records with latency
  context so teams can correlate quality with performance from day one.

Both workflows run deterministically over the runtime event log. Incremental
mode processes only new traces since the last checkpoint, making it safe to run
on a schedule.

```
/workflow run masher-trace-digest --input '{"mode": "trace", "target_agent_id": "..", "session_id": "..", "trace_id": ".."}'

/workflow run masher-online-eval-curation --input '{"mode": "incremental", "target_agent_id": ".."}'
```

## Self-Hosted Interfaces

Mash exposes agents over HTTP, supports streaming responses, includes a CLI and
REPL, and fits naturally into local, server, and containerized deployments. The
same platform can be used to develop an agent system, integrate it into product
surfaces, and operate it in a controlled environment. Checkout [Pilot Agent](https://github.com/imsid/mash-pilot/blob/main/README.md) for an implementation.

```bash
mash host serve --host-app <MASH_HOST> --port 8001
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
/trace [N]  Show trace analysis for recent traces
/workflow   List, run, and inspect workflows
```

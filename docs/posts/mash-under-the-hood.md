---
title: Mash Under the Hood
description: What Mash provides. One host over many agents, a durable harness, observability, and self-hosted interfaces.
date: 2026-06-10
author: imsid
tags:
  - product
  - overview
---

# Mash Under the Hood

The [product brief](product-brief.md) makes the pitch: applications should
integrate with agents through one standardized seam, and the host, not the
agent, is the unit of deploy. This post covers what Mash actually ships on
both sides of that seam: the host and its interfaces above, and the durable
harness and observability underneath.

- [One Host, Composable Agents](#one-host-composable-agents)
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
    - [Composing from the Outside](#composing-from-the-outside)
    - [REPL](#repl)
    - [CLI and REPL Commands](#cli-and-repl-commands)

## One Host, Composable Agents

Mash deployments are a flat pool of agents plus the host compositions defined
over them. A host names one agent as primary and a set of subagents; the
primary delegates to those specialists without a separate coordination layer
outside the runtime. Hosts can ship with the deploy in code, or be defined at
runtime over the API, and the same pool can serve several hosts at once.

```python
pool = (
    HostBuilder()
    .agent(PilotSpec(),   metadata=AgentMetadata(...))
    .agent(CliCopilot(),  metadata=AgentMetadata(...))
    .agent(ApiCopilot(),  metadata=AgentMetadata(...))
    .host(Host(host_id="pilot", primary="pilot", subagents=("cli-copilot", "api-copilot")))
    .build()
)
```

## Durable Harness

Mash provides the harness that durably executes a request: it is accepted,
executed through a durable request engine, and recorded as replayable runtime
events. That is a stronger operational model than a single in-memory agent
loop, and it makes retries, restarts, and long-running work easier to manage.

### Core Agent Loop: context, memory, tools, skills, signals, structured output

Each request runs a think → act → observe loop with context loaded from
memory, tools and skills available to it, and signals plus optional structured
output produced when it completes. Structured output gives integrations a
typed payload to consume instead of prose, which is what makes machine-readable
agent responses and predictable downstream automation practical.

Each agent brings its own `LLMProvider`, so different agents in the same host
can run on different models, a cheap model for triage and a capable one for
complex reasoning. Mash ships providers for Anthropic, Google Gemini, and OpenAI
out of the box. Each provider handles prompt caching differently: Anthropic uses
`cache_control` breakpoints on system and tool blocks, OpenAI uses explicit cache
keys with configurable retention, and Gemini creates server-side cached content
objects with a TTL. Mash abstracts all of this behind a single
`prompt_caching_enabled` flag in `AgentConfig` (on by default). The runtime
applies the right caching strategy for whichever provider the agent uses, so
repeated requests within a session avoid re-processing static context without
any provider-specific code from the developer.

Long-running sessions accumulate token cost as conversation history grows. Mash
handles this with automatic conversation compaction: when a session's total token
count crosses a configurable threshold, the runtime summarizes earlier turns into
a compact summary checkpoint using the agent's own LLM. Future requests load the
summary instead of the full history, keeping context size bounded without losing
key decisions, constraints, or user preferences. Compaction is configurable per
agent via `compaction_token_threshold` and `compaction_turn_limit` in
`AgentConfig`, and is disabled by default (threshold of 0).

Mash ships with runtime tools that are available to every agent out of the box:

- **BashTool**: execute shell commands in the host environment
- **AskUserTool**: pause execution to ask the user a question
- **InvokeSubagent**: delegate work to a registered subagent and stream its response back
- **search_conversations**: search stored conversation turns and return ranked previews
- **get_full_turn_message**: expand search results into full turn text

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
exactly where it left off when the user responds, whether that takes seconds or
hours.

Tool developers gate execution behind user consent by setting a single attribute
on the tool definition. Agents ask users questions by calling a built-in tool
that the runtime intercepts and translates into a durable interaction. Both paths
use the same protocol-level interaction events and the same host-to-client
response flow, so the operational model stays the same regardless of who
initiates the interaction.

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

When agents run as operational software, producing the right answer is not
enough; teams also need to know where the time went, down to the level of a
single tool call or a subagent delegation. Mash ships observability across the
runtime, API, UI, and CLI, so every trace is inspectable the moment it
completes, without wiring up a third-party APM or building a post-processing
pipeline first.

### Spans and Trace Analysis

Every request produces an ordered stream of `RuntimeEvent` records. Mash
transforms those flat events into a hierarchical **span tree** and computes a
deterministic **trace analysis** from that tree. No LLM inference is involved;
all metrics are derived directly from event timestamps and payloads.

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
  tool execution, subagent calls, and idle time, each with absolute and
  percentage values
- **Per-tool stats**: call count, total/avg/max/min latency, error count,
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
analysis dict in a single call, with no client-side computation needed.

### Telemetry UI

The built-in telemetry dashboard at `http://<HOST>/telemetry` renders traces
with a visual span waterfall. Each span is a collapsible row showing kind,
name, duration, and a proportional bar scaled to the trace total. A summary
bar at the top shows the time distribution across think, tool, subagent, cold
start, and idle phases. Below the waterfall, collapsible panels show tool stats,
step breakdown, and slowest operations tables.

### CLI Trace Inspection

The `/trace` REPL command gives developers access to trace analysis
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
  traces, and notable events: a complete diagnostic snapshot of one trace.
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

Mash exposes agents over HTTP, supports streaming responses, and includes a CLI
and REPL, so the same platform covers local development, integration into
product surfaces, and operating the system in a controlled environment. See
[Pilot Agent](https://github.com/imsid/mash-pilot/blob/main/README.md) for an
implementation.

The standard session: serve the pool, save the connection, compose a host,
and converse through it.

```bash
mash host serve --host-app <MASH_HOST> --port 8001
mash connect --api-base-url <MASH_HOST_URL> --api-key secret
mash compose --host pilot --primary pilot --subagents cli-copilot,api-copilot
mash repl
```

### Composing from the Outside

`mash compose` defines (or replaces) a host composition on the running
deployment with an idempotent `PUT /v1/hosts/{host_id}` and pins it as the
CLI's target. The same endpoint is open to any application, so an integration
can assemble its agent team at runtime without redeploying:

```bash
curl -X PUT <MASH_HOST_URL>/api/v1/hosts/pilot \
  -d '{"primary": "pilot", "subagents": ["cli-copilot", "api-copilot"]}'

curl -X POST <MASH_HOST_URL>/api/v1/hosts/pilot/request \
  -d '{"message": "...", "session_id": "s-1"}'
```

Each request snapshots the composition it was submitted with, so redefining a
host never disturbs work in flight. Telemetry events carry the `host_id`, so
traces filter by composition. The
[dynamic hosts guide](building-dynamic-hosts-apis.md) covers the full
API flow.

### REPL
The REPL is the interactive interface to a running host. It is pinned to the
composition (or bare agent) you connected with; to change teams, exit and run
`mash compose` again.

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

### CLI and REPL Commands

Top-level commands exposed by `mash`:

```
connect     Persist a deployment connection and target
compose     Define a host composition and target it
status      Show deployment status
agents      List pooled agents
hosts       List defined host compositions
sessions    List sessions for the target agent
history     Show session history
repl        Start the interactive shell
host serve  Run the host API server
```

Default slash commands inside the REPL:

```
/status     Show deployment, current host, agent, and session
/agents     List pooled agents
/hosts      List defined host compositions
/sessions   List remote sessions
/session    Show current session info
/history    View conversation history
/trace [N]  Show trace analysis for recent traces
/workflow   List, run, and inspect workflows
```

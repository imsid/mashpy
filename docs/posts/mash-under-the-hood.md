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
integrate with agents through a central host which is the unit of deploy. 
This post covers what Mash actually ships on
both sides of that seam: the host and its interfaces above, and the durable
harness and observability underneath.

- [One Host, Composable Agents](#one-host-composable-agents)
- [Durable Harness](#durable-harness)
    - [Core Agent Loop](#core-agent-loop)
    - [Human-in-the-Loop Interactions (HITL)](#human-in-the-loop-interactions-hitl)
    - [Workflows](#workflows)
- [Evals](#evals)
- [Observability](#observability)
    - [Spans and Trace Analysis](#spans-and-trace-analysis)
    - [Telemetry API](#telemetry-api)
    - [Admin dashboard](#admin-dashboard)
    - [CLI Trace Inspection](#cli-trace-inspection)
    - [Trace Digest](#trace-digest)
- [Self-Hosted Interfaces](#self-hosted-interfaces)
    - [Composing from the Outside](#composing-from-the-outside)
    - [REPL](#repl)
    - [CLI and REPL Commands](#cli-and-repl-commands)

## One Host, Composable Agents

Mash deployments are a pool of agents plus the host compositions defined
over them. A host names one agent as primary and a set of subagents; the
primary delegates to those specialists without a separate coordination layer
outside the runtime. A host can be composed from those agents in code at build
time, or at runtime over the API/CLI. This allows hosts to be [dynamic in their
composition](building-dynamic-hosts-apis.md) without requiring a deploy.

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

### Core Agent Loop

Every request executes inside a durable request: a think → act → observe loop
with context loaded from memory and tools and skills available to it, accepting
input from the CLI, the API, or a scheduled workflow, and producing signals and
optional structured output when it completes. The whole thing is resumable and
replayable across restarts. The diagram below maps that shape, and the rest of
this section walks through its parts.

```
                  ┌─────────────────────────────────────────┐
                  │          Durable Request                │  ◄── one request,
                  │                                         │      resumable and
                  │   ┌─ context ─── memory ──┐             │      replayable
request ────────► │   │                       │             │
(cli/api)         │   │     Agent Loop        │ ──► signals │
                  │   │ think → act → observe │      │      │
                  │   │                       │      ▼      │
                  │   └─ tools ───── skills ──┘  structured │
workflow ───────► │        ▲                      output    │
(schedule/trigger)│        │ user interaction               │
                  │        ▼ (approval / ask-user)          │
                  │                                         │
                  │       resumable · replayable            │
                  └─────────────────────────────────────────┘
```

**Frontier and OSS models**

Each agent brings its own `LLMProvider` configured via `build_llm()`. This allows different agents in the same host
to run on different models, a cheap model for triage and a capable one for
complex reasoning, an OSS model for privacy. Mash ships providers for frontier models from Anthropic, Google Gemini, and OpenAI
out of the box. 

Open-source models (OSS) run through the same harness with
`OSSCompatibleProvider`, which talks to any inference provider that expose a Chat-Completions compatible endpoint
That can be self-hosted vLLM, Ollama, or llama.cpp, or a hosted gateway like
Together, Groq, or OpenRouter. 

Mash ships providers for `GemmaProvider`, `QwenProvider`,
`DeepSeekProvider`, and `LlamaProvider` for the common families. The
model needs native tool calling so the runtime can pass tools and read back tool
calls, and swapping `build_llm()` is the only code change.

**Prompt cache**

Each provider handles prompt caching differently: Anthropic uses
`cache_control` breakpoints on system and tool blocks, OpenAI uses explicit cache
keys with configurable retention, and Gemini creates server-side cached content
objects with a TTL. Mash abstracts all of this behind a single
`prompt_caching_enabled` flag in `AgentConfig` (on by default). The runtime
applies the right caching strategy for whichever provider the agent uses, so
repeated requests within a session avoid re-processing static context without
any provider-specific code from the developer.

**Compaction**

Long-running sessions accumulate token cost as conversation history grows. Mash
handles this with automatic conversation compaction: when a session's total token
count crosses a configurable threshold, the runtime summarizes earlier turns into
a compact summary checkpoint using the agent's own LLM. Future requests load the
summary instead of the full history, keeping context size bounded without losing
key decisions, constraints, or user preferences. Compaction is configurable per
agent via `compaction_token_threshold` and `compaction_turn_limit` in
`AgentConfig`, and is disabled by default (threshold of 0).

**Tools**

Mash ships with runtime tools that are available to every agent out of the box:

- **BashTool**: execute shell commands in the host environment
- **AskUserTool**: pause execution to ask the user a question
- **InvokeSubagent**: delegate work to a registered subagent and stream its response back
- **search_conversations**: search stored conversation turns and return ranked previews
- **get_full_turn_message**: expand search results into full turn text

**Web search**

Web search gives your agents the ability to extract data from the web. To turn it on, you must explicitly specify a
provider by returning a `WebSearchProvider` via `build_web_search()`. There's no default provider intentionally, so you
always know who is handling your search data. Mash ships one provider,
`ParallelSearchProvider` from ParallelAI, which offers `web_search` and `web_fetch` tools and requires
an API key.

**Feedback**

The REPL ships a `/feedback` command. A user types `/feedback <message>` and the
note is captured as written, with no LLM step, stored alongside the host, agent,
session, and last request id from the current shell. It lands in a
`runtime_feedback` table in the runtime store, next to the event log. App
developers read it back over the API with `GET /api/v1/feedback`, which takes a
required `agent_id` and `after` timestamp plus optional `before`, `session_id`,
`feedback_type`, and `q` full-text filters. Submission has its own route,
`POST /api/v1/feedback`. Neither endpoint depends on observability being enabled.

**Persistence**

Mash persists to Postgres, and three areas write their own tables: the runtime
store behind the loop's resumability and observability, the per-agent memory
store behind context loading, and the backend API request log. The main tables:

| Table | Store | Purpose |
|---|---|---|
| `runtime_event_log` | runtime | Append-only, ordered event stream for every request. The runtime replays from it across restarts, and trace analysis and the telemetry views are computed from it. |
| `runtime_feedback` | runtime | Notes submitted through `/feedback`, read back through the feedback API. |
| `memory_turns` | memory | One row per turn (user message, agent response, running token total). The replayable turns are the conversation history context loading feeds into a request; workflow and subagent turns are stored here too but excluded from replay. |
| `memory_signals` | memory | Named signals emitted on a turn, stored as values keyed to that turn. |
| `api_event_log` | api | HTTP request and response log for the backend API: method, path, status, duration, and bodies. |

### Human-in-the-Loop Interactions (HITL)

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

A workflow is an ordered sequence of tasks, JSON in and JSON out where each task is wired to an agent and optionally a structured
output schema. Workflow can be deployed via code and composed in the host or registered dynamically at runtime via
the host API or a client. Under the hood a workflow run is invoked with a fixed input payload that the runtime
routes to the task agent as a JSON request:

```json
{ "workflow_id": "...", 
  "workflow_run_id": "...", 
  "task_id": "...",
  "workflow_input": { ... }, 
  "task_state": { ... } 
}
```

The agent reads `workflow_input` and the prior `task_state`, and its structured
output becomes that task's persisted state for future runs and downstream steps.

A code-deployed workflow gets a dedicated workflow agent by passing an
`agent_spec`, which `HostBuilder.workflow()` registers as a workflow-only agent
hidden from delegation and listings. That agent carries the task's skill in its
own `build_skills()`, and the `WorkflowSpec` ships with the pool at build time:

```python
from mash.workflows import TaskSpec, WorkflowSpec

quiz_workflow = WorkflowSpec(
    workflow_id="pilot-quiz",
    tasks=[TaskSpec(task_id="run-quiz", agent_spec=QuizAgentSpec(...))],
)
# HostBuilder().agent(PilotSpec(), metadata=...).workflow(quiz_workflow)
```

A dynamically registered workflow repurposes an existing agent instead of adding
one. The client registers a dynamic `Skill` holding the task instructions, then a
workflow whose task points at that skill through a `task_message`, and runs it:

```python
client.register_agent_skill("pilot", {
    "type": "dynamic",
    "name": "workflow:pilot-changelog",
    "description": "Generate a changelog from recent git commits.",
    "content": skill_markdown,
})
client.register_agent_workflow("pilot", {
    "workflow_id": "pilot-changelog",
    "tasks": [{"task_id": "scan-recent-commits", "agent_id": "pilot",
               "structured_output": {...}}],
    "task_message": {"skill_name": "workflow:pilot-changelog"},
})
client.run_workflow("pilot-changelog", workflow_input={"commit_count": 5})
```

The same pair is exposed via the Host API as
`POST /api/v1/agent/{agent_id}/skill` and `POST /api/v1/agent/{agent_id}/workflow`,
so a service can generate a skill, publish a workflow that loads it, and trigger
runs without redeploying. 

Dynamic definitions live in memory, so the owning
application republishes them after a restart. This makes workflows a concrete
execution model for repeatable, stateful agent tasks rather than a loose
orchestration layer.


## Evals

The host is the unit of deploy and the unit of evaluation. Mash evals run a
dataset through the full composition, primary agent, delegation, and
subagents, so what gets scored is the response the application actually
receives. Synthetic evals ship as two workflows on Masher, Mash's built-in
workflow agent, registered into every pool by default (opt out with
`HostBuilder.enable_masher(False)`). Masher is a hidden workflow-only worker:
it never appears in agent listings or `InvokeSubagent` delegation.

- **`gen-synthetic-evals`** reads a host's declared capabilities and the
  developer's guidance and generates the eval: a dataset of test scenarios
  (default 20 rows, max 100) plus a weighted scoring rubric. The rubric stays
  editable until the first experiment runs; from then on the eval is locked,
  which is what keeps experiments comparable.
- **`score-evals`** runs one experiment. It snapshots the live host
  composition and the spec of every agent in it, fans the dataset rows out as
  durable child workflows over a dedicated queue, judges each output with
  Masher against the rubric, and folds each row's session events into
  operational metrics.

Each run records two kinds of signal. Deterministic quantitative metrics come
from the row's runtime events: latency, tokens with the cached read/write
split, steps, tool calls, per-subagent breakdowns. Qualitative criteria are
non-deterministic and scored by the Masher LLM judge, each with a rationale.
Comparison is computed at read time over any two experiments of the same
eval: the agent spec delta from the snapshots, score movement per criterion
and per row, and the operational delta side by side. Nothing derived is
stored.

The admin dashboard's Evals tab drives the loop end to end: generate an eval
for a host, tune the rubric, run experiments, and compare two of them down to
individual rows. [Synthetic evals](synthetic-evals.md) covers the design in
full.

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

### Admin dashboard

The built-in admin dashboard at `http://<HOST>/admin` is a read view over the
running deployment.

Overview reports deployment health: counts of agents, hosts, and sessions, plus
a per-day chart of requests and tokens across the pool.

Agents lists the role-less pool as cards. Each card shows the agent's display
name, description, capabilities, usage guidance, and the hosts that use it as
primary or subagent. Clicking a card opens that agent's logs.

Tools and Skills list everything registered across the agent pool, each with a
detail view showing which agents carry it.

Workflows lists the workflows registered in the pool with the task chain each
one runs, every task next to the agent that runs it.

Hosts shows the host compositions: which agent is primary and which are
subagents for each host.

Evals drives the eval loop: generate an eval for a host, inspect its dataset
and rubric, run experiments, and compare two experiments down to per-row score
movement and each row's baseline and control responses.

Logs is the trace view. Sessions list for the selected agent. Expand a session
to see its traces newest first, then click a trace to open a drawer. The drawer
has summary tiles for duration, tokens, output tokens per second, and tool
count; the signals collected on that turn; the conversation reconstructed
message by message and rendered as markdown; a collapsible span tree with
per-span durations; and the raw events behind a filter by event type. A
separate API access tab lists the backend HTTP request logs newest first.

Feedback shows the notes captured by the REPL's `/feedback` command. Reference
embeds the API and CLI documentation so it stays next to the deployment it
describes.

### CLI Trace Inspection

The `/trace` REPL command gives developers access to trace analysis
without leaving the terminal:

```bash
/trace        # analyze the most recent trace
/trace 5      # analyze the 5 most recent traces
```

Each trace renders a summary line (status, duration, steps, tool calls, tokens),
a timing breakdown table with visual bars, per-tool stats, and the top slowest
operations.

### Trace Digest

Masher also exposes `masher-trace-digest`, a workflow that builds on the same
span and analysis infrastructure. It produces a schema v2 digest with the full
latency breakdown, tool stats, step breakdown, slowest operations, nested
subagent traces, and notable events: a complete diagnostic snapshot of one
trace. The workflow runs deterministically over the runtime event log, and
incremental mode processes only new traces since the last checkpoint, making
it safe to run on a schedule.

```bash
/workflow run masher-trace-digest --input '{"mode": "trace", "target_agent_id": "..", "session_id": "..", "trace_id": ".."}'
```

## Self-Hosted Interfaces

Mash exposes agents over HTTP, supports streaming responses, and includes a CLI
and REPL, so the same platform covers local development, integration into
product surfaces, and operating the system in a controlled environment. See
[`src/pilot/`](https://github.com/imsid/mashpy/tree/main/src/pilot/) for an
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

```bash
host serve  # Run the host API server
connect     # Persist a deployment connection and target
status      # Show deployment status
browse      # Browse the pool: agents, workflows so you can
            # see what is available to wire into a host before composing one.
compose     # Define a host composition
agents      # List pooled agents
hosts       # List defined host compositions
sessions    # List sessions for the target agent
history     # Show session history
repl        # Start the interactive shell
```

`mash browse` is the one-shot tour of a deployment: it lists the agent pool, the
pool-wide workflow registry (unfiltered by host — this is where built-in
workflows like Masher's surface), and the defined host compositions, so you can
see what is available to wire into a host before composing one.

Default slash commands inside the REPL:

```bash
/status     # Show deployment, current host, agent, and session
/agent      # List pooled agents
/host       # List defined host compositions (with attached workflows)
/sessions   # List remote sessions
/session    # Show current session info
/history    # View conversation history
/trace [N]  # Show trace analysis for recent traces
/workflow   # List, run, and inspect workflows (bare /workflow lists)
/feedback   # Record a note or bug report about this session
```

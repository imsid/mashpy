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
    - [Sandboxes](#sandboxes)
    - [Human-in-the-Loop Interactions (HITL)](#human-in-the-loop-interactions-hitl)
    - [Workflows](#workflows)
- [Evals](#evals)
- [Observability](#observability)
    - [Spans and Trace Analysis](#spans-and-trace-analysis)
    - [Telemetry API](#telemetry-api)
    - [Admin dashboard](#admin-dashboard)
    - [CLI Trace Inspection](#cli-trace-inspection)
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

When the host starts, it runs each agent in the pool as its own subprocess
(`mash agent serve`) on a localhost port and keeps an HTTP client for it. The
host restarts a process that exits, and `POST /api/v1/agent/{agent_id}/disable`
and `.../enable` stop and start one on a running host.

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
replayable across restarts. The code the model writes leaves the request for
the sandbox of the user it acts for, and the browser tools drive that user's
browser. The diagram below maps that
shape, and the rest of this section walks through its parts.

```
                  ┌─────────────────────────────────────────┐
                  │          Durable Request                │  ◄── one request,
                  │                                         │      resumable and
                  │   ┌─ context ─── memory ──┐             │      replayable
                  │   │                       │             │
request ────────► │   │     Agent Loop        │ ──► signals │
(cli/api)         │   │ think → act → observe │      │      │
                  │   │                       │      ▼      │
                  │   └─ tools ───── skills ──┘  structured │
workflow ───────► │      │ ▲                      output    │
(schedule/trigger)│      │ │ user interaction               │
                  │      │ ▼ (approval / ask-user /         │
                  │      │    browser sign-in)              │
                  │      │                                  │
                  │      │  resumable · replayable          │
                  └──────┼──────────────────────────────────┘
                         ├────────────────────────────┐
                         │ run_code                   │ browser_*
                         ▼                            ▼
                  ┌──────────────────────────┐ ┌──────────────────────────┐
                  │  Sandbox: one per user   │ │  Browser: one per user   │
                  │  workspaces/<agent_id>   │ │  Chromium with a profile │
                  │  files persist           │ │  listed sites only       │
                  │  no secrets · no db      │ │  sign-in via live view   │
                  └──────────────────────────┘ └──────────────────────────┘
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
keys with configurable retention, and Gemini caches implicitly, matching the
leading tokens of each request against recent ones with nothing to configure.
Mash abstracts all of this behind a single `prompt_caching_enabled` flag in
`AgentConfig` (on by default). The runtime applies the right caching strategy for
whichever provider the agent uses, so repeated requests avoid re-processing
static context without any provider-specific code from the developer.

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

- **run_code**, **sandbox_put**, **sandbox_get**: run Python, JavaScript or shell code in the sandbox of the user the request acts for when the host has sandboxes; the two file tools copy files in and out of folders the spec declares, and exist only when it declares some
- **browser_navigate**, **browser_snapshot**, **browser_click** and the rest of Playwright MCP's page tools, plus **browser_save**, **browser_upload** and **browser_handoff**: drive the browser of the user the request acts for, on the sites the spec lists with `browser_sites()`, and exist only for a spec that lists some
- **AskUserTool**: pause execution to ask the user a question
- **RunSubAgents**: run one or more requests at the same time on registered subagents or the agent itself, and return their results in order
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

Mash persists to Postgres, and four areas write their own tables: the runtime
store behind the loop's resumability and observability, the per-agent memory
store behind context loading, the workflow store behind workflow runs, and the
backend API request log. The main tables:

| Table | Store | Purpose |
|---|---|---|
| `runtime_event_log` | runtime | Append-only, ordered event stream for every request. The runtime replays from it across restarts, and trace analysis and the telemetry views are computed from it. |
| `runtime_feedback` | runtime | Notes submitted through `/feedback`, read back through the feedback API. |
| `memory_turns` | memory | One row per turn (user message, agent response, running token total). The replayable turns are the conversation history context loading feeds into a request; workflow and subagent turns are stored here too but excluded from replay. |
| `memory_signals` | memory | Named signals emitted on a turn, stored as values keyed to that turn. |
| `workflow_runs`, `workflow_steps`, `workflow_step_events` | workflow | One row per workflow run, per-step input/output snapshots, and the append-only step lifecycle audit behind durable resume and the step audit trail. |
| `api_event_log` | api | HTTP request and response log for the backend API: method, path, status, duration, and bodies. |

### Sandboxes

Every request acts for a user: submit bodies carry a required `user_id`, which
the application asserts and the host trusts. Each user gets a sandbox, a
container the host starts through the Docker socket the first time an agent
runs code for that user. It is the user's computer: Mash's sandbox image with
Python, Node, SQLite and git. When the host has sandboxes, every agent gets
`run_code`, and an agent whose spec declares file roots also gets
`sandbox_put` and `sandbox_get`. They post to the host's `/sandbox`
relay with the agent's token and the request id, and the host picks the user
from the request, so an agent cannot choose whose sandbox it runs in.

```text
agent process ──► host /sandbox/exec ──► mash-sandbox-<key> (one per user)
  run_code          request → user          /home/agent/workspaces/<agent_id>
```

Inside, a small daemon runs each piece of code as the unprivileged `agent` user
with a clean environment, in the agent's folder at
`/home/agent/workspaces/<agent_id>`. Each call is a new process, and files,
SQLite databases and `pip install --user` packages persist on the sandbox's
volume across requests and restarts. The model hands the user what its code
made through links the host serves: a file opens at `$MASH_FILES_URL/<path>`
and a server the code started at `$MASH_PORTS_URL/<port>/`. The sandbox holds
only that user's files; credentials, MCP connections and the database stay in
the agent processes on the host. A sandbox stops after
`MASH_SANDBOX_IDLE_SECONDS` (default 900) without use, at most
`MASH_SANDBOX_MAX_RUNNING` (default 20) run at once, and `mash sandbox ls`,
`shell` and `stop` manage them from the Docker host. Without a Docker daemon the
host still runs, and its agents get no sandbox tools.

### Browsers

Each user also gets a browser: a Chromium in a container of its own,
`mash-browser-<key>`, next to the user's sandbox and on a separate Docker
network. Its profile, on the container's volume, keeps the sites the user
signed in to, and every agent acting for that user shares it. The host starts
browsers only when some agent's spec lists sites with `browser_sites()`, and
only those agents get the browser tools.

```text
agent process ──► host /browser/call ──► mash-browser-<key> (one per user)
  browser_click     request → user,        daemon ─► Playwright MCP ─► Chromium
                    sites from the spec    one MCP session and tab per request

user ───────────► /s/<view token>/browser/ ──► noVNC live view of the same Chromium
```

The tools post to the host's `/browser` relay with the agent's token and the
request id, like the sandbox tools, and the host adds the user and the spec's
sites. Inside the container, a daemon runs Chromium headed on a virtual
display, with Playwright MCP 0.0.83 attached to it over CDP. The daemon is
Playwright MCP's client: each request gets its own MCP session and its own
tab. It checks every URL against the sites before a call runs, and when a
click or redirect lands outside them it goes back and returns an error
instead of the page. Page text comes back between `<web-content>` tags, so the
model reads it as data from the site. `browser_take_screenshot` returns an
image the model sees, and the runtime keeps the three most recent images in
context.

Downloads land in the browser container, and `browser_save` copies one into
the agent's sandbox folder, where `run_code` reads it. `browser_upload` goes
the other way, from the folder to a file input. The browser holds the user's
signed-in sessions, so the sandbox has no route to it: the sandboxes are not
on the browsers' network and hold no browser token.

A sign-in uses `browser_handoff`. The engine opens a durable interaction of
type `browser` whose payload carries `url`, a live-view link, and
`expires_at`. The link is a view token valid for
`MASH_BROWSER_HANDOFF_SECONDS` (default 900), and it opens noVNC in the
user's own browser, showing the request's tab. The user signs in, answers `done`, and the request resumes with the
session in the profile. The token is revoked when the interaction ends. In a
workflow run, `browser_handoff` fails at once, since no one is there to sign
in; workflow code can drive the same browser with Playwright through
`ctx.browser()`.

A browser stops after `MASH_BROWSER_IDLE_SECONDS` (default 900) without use,
at most `MASH_BROWSER_MAX_RUNNING` (default 10) run at once, and
`mash browser ls`, `view` and `stop` manage them from the Docker host. Apps
list and clear a user's signed-in sites under
`/api/v1/browser/users/{user_id}`.

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

A workflow is an ordered step pipeline defined in Python and registered with
the pool at build time. Each step is either a `CodeStep`, a deterministic
Python function, or an `AgentStep`, one run of a registered agent's loop.
Steps declare pydantic input and output models. Each step's output merges over
the run's immutable `workflow_input` to form the next step's input, and the
final step's output is the run result. The pipeline is validated when the pool
is built, so a step that expects a field no earlier step provides fails the
build.

```python
from mash.workflows import AgentStep, CodeStep, WorkflowSpec

changelog = WorkflowSpec(
    workflow_id="changelog",
    input_model=ScanIn,
    steps=[
        CodeStep(step_id="scan", run=scan, input=ScanIn, output=ScanOut),
        AgentStep(step_id="summarize", agent_id="writer",
                  input=ScanOut, output=SummaryOut),
    ],
)
# HostBuilder().agent(WriterSpec(), metadata=...).workflow(changelog)
```

An agent step executes as a normal durable request. The agent receives a JSON
envelope carrying `workflow_id`, `workflow_run_id`, `step_id`,
`workflow_input`, and its coerced `input`, and must return structured output
matching the step's output schema. The step can target a pooled agent by id or
carry its own `agent_spec`, which registers as a workflow-only agent hidden
from listings and delegation.

Runs are durable and observable. DBOS orchestrates each run, a failed run
resumes from the failed step with completed steps replayed from their stored
outputs, and every step leaves an audit trail of status, input and output
snapshots, and lifecycle events in dedicated workflow tables. Runs start and
stream over the API (`POST /api/v1/workflow/{id}/run`, then SSE on the run's
events endpoint) or the REPL (`/workflow run|status|resume`). Code steps can
express dynamic control flow such as the async row fan-outs in
`run-experiment`.
[Workflows as Step Pipelines](workflows-as-step-pipelines.md) covers the layer
in full.


## Evals

The host is the unit of deploy and the unit of evaluation. Mash evals run a
dataset through the full composition, primary agent, delegation, and
subagents, so what gets scored is the response the application actually
receives. Synthetic evals ship as two Masher workflows backed by Mash's built-in
generation and judging agents. `HostBuilder.enable_evals()` registers both
agents and both workflows; their specs then appear in agent listings.

- **`gen-synthetic-evals`** reads a host's declared capabilities and the
  developer's guidance and generates the eval: a dataset of test scenarios
  (default 20 rows, max 100) plus a weighted scoring rubric. The rubric stays
  editable until the first experiment runs; from then on the eval is locked,
  which is what keeps experiments comparable.
- **`run-experiment`** snapshots the live host, AgentSpecs, rubric, and dataset
  into a durable row ledger. Two CodeSteps fan unfinished rows out through the
  host and the dedicated eval judge, persisting output and metrics after each
  phase.

Each run records two kinds of signal. Deterministic quantitative metrics come
from the row's runtime events: latency, tokens with the cached read/write
split, steps, tool calls, per-subagent breakdowns. Qualitative criteria are
non-deterministic and scored by the eval agent, each with a rationale.
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

Every request produces an ordered stream of `RuntimeEvent` records, each named
after the OpenTelemetry span it starts or ends. Mash projects those flat events
onto **OpenTelemetry spans and log records**, with GenAI names and attributes,
and computes a deterministic **trace analysis** from them. No LLM inference is
involved; all metrics are derived directly from event timestamps and payloads.
The same projection is what the host exports over OTLP.

The projection models the full request lifecycle:

```
invoke_agent assistant
├── mash.cold_start              (accepted → context load starts)
├── mash.context_load            (history and summary loaded)
├── mash.step 0
│   ├── chat claude-sonnet-4-6   (model call, with token usage)
│   ├── execute_tool run_code    (200ms)
│   └── execute_tool read_file   (150ms)
├── mash.step 1
│   ├── chat claude-sonnet-4-6
│   └── execute_tool RunSubAgents
│       └── invoke_agent research   (3.2s, the child in this trace)
└── mash.step 2
    └── chat claude-sonnet-4-6   (final response)
```

The trace analysis computed from these spans includes:

- **Timing breakdown**: total duration, cold start, context load, model calls,
  tool execution, subagent calls, and idle time, each with absolute and
  percentage values
- **Per-tool stats**: call count, total/avg/max/min latency, error count,
  sorted by total time so the most expensive tool surfaces first
- **Per-step breakdown**: model call, tool, and overhead time for each agent loop
  iteration
- **Slowest operations**: top 10 spans ranked by duration
- **Subagent traces**: each `RunSubAgents` child is nested under the call that
  started it and analyzed on its own

### Telemetry API

The telemetry API exposes both raw events and structured analysis:

```
GET /telemetry/events                  # flat runtime events
GET /telemetry/traces                  # list recent traces (lightweight)
GET /telemetry/trace/analysis          # OpenTelemetry projection + latency analysis
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
mash connect --api-base-url <MASH_HOST_URL> --api-key secret --user-id u-1
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
  -d '{"message": "...", "session_id": "s-1", "user_id": "u-1"}'
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

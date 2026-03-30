# Background Agents

Status: Draft

Version: 0.1.0

Last Updated: 2026-03-30

## 1. Overview

Mash already has a strong primitive for specialist agents: hosted subagents invoked through `InvokeSubagent`. This RFC extends that model with background execution so a specialist can maintain grounded derived state asynchronously while still answering targeted questions on demand.

The motivating examples are:

- `masher`: after each primary-agent trace completes, analyze the run in the background and persist grounded analysis into the target app state store so later questions can be answered quickly.
- `historian`: maintain an incremental summary of what happened in prior sessions and in the current session so primary agents can ask questions like "what happened last session?" or "did the user already discuss this topic before?"

The design is constrained by three requirements:

- background tasks must never block the primary agent
- targeted question-answering must stay separate from background maintenance work
- each background agent must have an explicit, confined task boundary

## 2. Goals

- Reuse the existing hosted-agent and memory-store architecture.
- Avoid reprocessing the same trace or session repeatedly.
- Keep answers grounded in persisted turns, traces, logs, and app data.
- Make background agents discoverable to primaries through the existing subagent routing model.

## 3. Non-Goals

- This RFC does not introduce a general distributed job system.
- This RFC does not add a new memory-store backend or new storage tables by default.
- This RFC does not allow background agents to answer arbitrary open-ended questions outside their contract.
- This RFC does not require every subagent to become a background agent.

## 4. Proposed Model

### 4.1 Logical Background Agent

A background agent is a specialized subagent with two execution lanes:

- `interactive lane`: the existing foreground subagent path used by `InvokeSubagent(agent_id, prompt, opts)`
- `maintenance lane`: a silent host-managed path used only for registered background tasks

Both lanes belong to one logical agent id and share the same bounded contract, but they do not share execution capacity.

### 4.2 Separate Runtime Lane

The maintenance lane should run in a separate runtime instance from the interactive lane.

Rationale:

- `MashAgentServer` currently processes requests through a single worker queue.
- Reusing that queue for silent maintenance would let background work delay interactive queries.
- A separate maintenance runtime preserves the existing request semantics and gives hard isolation with minimal conceptual change.

In practice, one logical background agent becomes:

- one visible hosted subagent runtime for foreground invocation
- one hidden hosted runtime for background maintenance jobs

The primary agent only sees the visible subagent runtime.

## 5. Contracts And Boundaries

Each background agent must declare two explicit contracts.

### 5.1 Query Contract

The query contract defines the questions the agent may answer when invoked by the primary.

Examples:

- `masher`
  - analyze a specific run
  - compare recent runs
  - explain tool, model, or latency behavior for a trace
- `historian`
  - summarize the last session or current session so far
  - answer whether a topic was discussed in previous sessions
  - report prior outcomes, decisions, blockers, or TODOs tied to prior sessions

If a request falls outside the query contract, the agent must decline and state that the question is outside its scope.

### 5.2 Background Task Contract

The background task contract defines the only silent tasks the host may enqueue for the agent.

Each task should be registered as a deterministic `task_id` with:

- description
- trigger
- source scope
- output keys written to state
- idempotency strategy

Suggested type:

```python
@dataclass(frozen=True)
class BackgroundTaskSpec:
    task_id: str
    description: str
    trigger: Literal["trace_completed", "session_started", "host_startup"]
    source_scope: Literal["trace", "session", "app"]
    output_keys: list[str]
```

The runtime must reject any maintenance task whose `task_id` is not declared by that agent.

## 6. Host Responsibilities

The host is responsible for turning primary-agent activity into background work.

### 6.1 Registration

Add a background-agent registration path alongside existing subagent registration.

Suggested shape:

```python
MashAgentHostBuilder().background_agent(
    spec,
    metadata=...,
    background_tasks=[...],
)
```

`enable_masher()` can later become a thin convenience wrapper over this path.

### 6.2 Triggering

The host should enqueue maintenance work after the primary turn has been persisted.

The important trigger points are:

- `trace_completed`: after a primary trace finishes and its turn is saved
- `session_started`: optionally enqueue catch-up or prior-session rollups
- `host_startup`: optionally enqueue bounded backfill work for stale sessions

The trigger payload should include:

- `primary_app_id`
- `source_session_id`
- `source_trace_id`
- `turn_id`
- `session_total_tokens`
- selected response metadata and signals

### 6.3 Delivery Semantics

Background submission is fire-and-forget:

- no streamed events to the primary agent
- no synchronous wait on completion
- failures are logged but do not fail the primary request

## 7. Persistence And Idempotency

No new memory-store abstraction is required for v1.

### 7.1 Reuse Existing Store Capabilities

Use existing store APIs and tools:

- `save_turn` / `get_turns`
- `list_sessions` / `get_latest_session`
- `list_recent_traces` / `get_latest_trace`
- `get_logs` and log-backed trace tools
- `search_conversations`
- `get_turn_by_ids`
- `get_app_data` / `set_app_data` / `list_app_data`
- persisted `signals`

### 7.2 State Layout

Derived state should be stored in the target app's `app_data`, not only in the background agent's own conversation history.

Recommended key convention:

- `background/<agent_id>/cursor`
- `background/<agent_id>/session_summary`
- `background/<agent_id>/trace/<trace_id>`

For example, the historian can store this under the primary session:

```json
{
  "version": 1,
  "agent_id": "historian",
  "last_processed_trace_id": "trace_123",
  "topics": ["background agents", "session memory"],
  "summary": "The user designed background agents and decided to separate interactive and maintenance lanes.",
  "outcomes": [
    "Background agents stay bounded to explicit task contracts.",
    "Maintenance work should persist derived state into app_data."
  ],
  "open_questions": [],
  "source": {
    "session_id": "sess_1",
    "turn_count": 8
  }
}
```

### 7.3 Incremental Processing

Background tasks must be incremental.

The canonical cursor is the last processed trace id, because:

- `trace_id` is already persisted as `turn_id`
- traces are stable and correlated to logs
- the store already exposes chronological turn history

Algorithm:

1. read `background/<agent_id>/cursor` from `app_data`
2. inspect current session turns or recent traces
3. process only traces after `last_processed_trace_id`
4. update the derived artifact
5. persist the new cursor atomically after successful write

If there is no cursor, the task performs a first-pass summary for that session.

### 7.4 Deduping

The host should dedupe queued/running maintenance jobs by:

- `agent_id`
- `task_id`
- `source_session_id`
- `source_trace_id` when applicable

This prevents repeated enqueue storms during rapid turn completion.

## 8. Interactive Queries Versus Maintenance Tasks

Interactive queries and background maintenance must be independent.

### 8.1 Interactive Query Path

When the primary invokes a background agent:

1. the request goes to the interactive lane
2. the agent answers from the latest committed derived state first
3. if needed, it supplements with direct retrieval from turns, traces, logs, or search
4. it does not wait for queued maintenance work

If the derived state is stale, the agent should answer with an explicit freshness note and may request that the host enqueue a catch-up maintenance task.

### 8.2 Maintenance Path

When the host enqueues a background task:

- the request goes only to the maintenance lane
- the prompt is system-generated, not user-generated
- the task writes derived state and exits
- no foreground response is produced

This separation is the core guarantee that requirement 1 and requirement 2 are both satisfied.

## 9. Grounding Rules

Background-agent answers must stay grounded in persisted sources.

Allowed grounding sources:

- persisted turns
- trace metadata
- structured logs
- stored signals
- derived `app_data` written by the same agent

Required answer behavior:

- prefer precomputed state when available
- cite the supporting session ids, trace ids, or turn ids in the answer payload
- distinguish direct facts from inference
- refuse unsupported questions outside the contract

## 10. Minimal Runtime Surface Changes

This design intentionally keeps changes small.

### 10.1 New Runtime Concepts

Add host/runtime types for:

- `BackgroundTaskSpec`
- `BackgroundAgentMetadata`
- `BackgroundTaskRequest`

`BackgroundAgentMetadata` should extend current subagent metadata with:

- `query_scope`
- `background_tasks`

### 10.2 New Host Wiring

Add host support for:

- registering a logical background agent
- creating both interactive and maintenance runtimes
- enqueueing maintenance work on trigger
- best-effort deduping

### 10.3 Tooling Changes

Do not add new store methods unless implementation proves it is necessary.

For v1, the likely tool additions are wrappers over existing store APIs only:

- `list_sessions`
- `get_session_app_data`

These are sufficient for the historian to inspect prior sessions without inventing a new persistence layer.

## 11. Agent Designs

### 11.1 Masher

Boundaries:

- only answers run-analysis and eval-curation questions
- only analyzes configured event-store data
- only performs background tasks declared in its contract

Suggested background tasks:

- `analyze_trace`
  - trigger: `trace_completed`
  - input: `source_session_id`, `source_trace_id`
  - output: `background/masher/trace/<trace_id>`

Suggested query behavior:

- resolve target trace
- check for cached trace analysis first
- fall back to raw log analysis if the cache is missing or stale

### 11.2 Historian

Suggested agent id: `historian`

Boundaries:

- only answers questions about prior sessions, prior discussion topics, prior outcomes, and current-session recap
- does not answer general product or domain questions
- does not invent prior outcomes when retrieval is weak

Suggested background tasks:

- `summarize_session_delta`
  - trigger: `trace_completed`
  - input: `source_session_id`, `source_trace_id`
  - output: `background/historian/session_summary`
- `backfill_session_summary`
  - trigger: `host_startup` or interactive miss
  - input: `source_session_id`
  - output: `background/historian/session_summary`

Suggested retrieval flow for interactive queries:

1. determine target sessions with `list_sessions`, `get_latest_session`, and `search_conversations(scope="app")`
2. read stored session summaries from `app_data`
3. expand supporting turns with `get_turn_by_ids` when needed
4. answer with explicit provenance and freshness

Example questions:

- "What happened in the last session?"
- "Did the user already discuss background agents before?"
- "What was the outcome of the earlier conversation about memory search?"
- "Summarize this session so far."

## 12. Failure And Recovery

Background-agent failure should degrade gracefully.

- Primary requests still succeed even if background maintenance fails.
- Failed maintenance jobs should be visible in logs and may be retried on the next trigger.
- Incremental cursors prevent duplicate work after partial success.
- On startup, the host may enqueue bounded catch-up jobs for sessions whose cursor is behind the latest trace.

## 13. Why This Design

This design is intentionally conservative.

- It reuses the existing hosted-agent architecture.
- It avoids changing the memory-store protocol.
- It gives hard non-blocking behavior by separating runtime lanes.
- It keeps background agents trustworthy by requiring explicit task contracts and grounded storage.

The result is a general background-agent model that fits both `masher` and `historian` without introducing a second memory system or a generic unbounded worker framework.

# Workflows

`src/mash/workflows` is a DBOS-backed workflow layer on top of the Mash agent
runtime. A workflow guarantees the execution of a deterministic, ordered set of
steps, is durable (a run resumes from the failed step), and is observable (a
per-step audit trail in a dedicated store).

A workflow runs as either:

- a **step pipeline** — an ordered `steps` list, the default; or
- a **strategy** — a `WorkflowStrategy` that owns its own DBOS registration and
  run body, for non-linear shapes (fan-out, branching). See
  [`strategy.py`](./strategy.py) and the eval `ScoreEvalsStrategy`.

## Steps

Each step is one of:

- **`CodeStep`** — deterministic Python: `run(inp, ctx) -> output`, sync or async.
  Pydantic-typed on both edges. Authored in Python only.
- **`AgentStep`** — one run of a registered agent's loop. `output` may be a
  pydantic model or a JSON-schema dict; either becomes the request's
  structured-output schema. `input` may be a pydantic model or `None`
  (passthrough). An optional `skill_name` tells the agent to load a skill first.

Step *n*'s `output` threads into step *n+1*'s `input`, merged over the immutable
`workflow_input`. The final step's output is the run result. `WorkflowSpec`
validates the pipeline at build time (unique ids, typed I/O, and — when
`input_model` is set — field-level adjacency between steps).

```python
from pydantic import BaseModel
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec


class ScanIn(BaseModel):
    repo_url: str

class ScanOut(BaseModel):
    files_changed: list[str]
    head_sha: str

class SummaryOut(BaseModel):
    summary: str
    head_sha: str


def scan(inp: ScanIn, ctx: StepContext) -> ScanOut:
    ...


CHANGELOG = WorkflowSpec(
    workflow_id="changelog",
    input_model=ScanIn,
    steps=[
        CodeStep(step_id="scan", run=scan, input=ScanIn, output=ScanOut),
        AgentStep(step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut),
    ],
)
```

Register with `HostBuilder().workflow(CHANGELOG)`. Agent-step agents are
auto-registered from their `agent_spec`.

## Idempotency

Step execution is at-least-once (DBOS recovery re-runs an interrupted step).
Pure transforms replay safely. A step with external effects must dedupe on a
stable key — `StepContext` carries `run_id`, `step_id`, `workflow_input`, and
`attempt`, all stable across retries. The framework never invents a key or
classifies steps.

## Durability and resume

DBOS orchestrates and recovers runs. Each step body and each store write is its
own memoized DBOS step, so a replay skips completed work; store writes are
idempotent so the crash-after-effect window converges rather than duplicating.

- `resume_run(run_id)` — replay completed steps and re-drive from the failed
  step (same `run_id`). Agent steps interlock with their own durable request
  workflow through a deterministic `request_id`, so they resume mid-loop.
- `run_workflow(workflow_id, ...)` — a fresh `run_id` from step 1.

A step may declare `timeout_s`; exceeding it fails the step (resumable), not a
retry. When DBOS recovery attempts are exhausted the run is `failed`.

## Storage

Three tables (in the shared schema baseline `001_baseline.sql`), owned by the
workflow layer:

- `workflow_runs` — one row per run (status, `workflow_input`, `result`, timing).
- `workflow_steps` — one row per step (status, input/output snapshots, attempt,
  `agent_request_id`).
- `workflow_step_events` — append-only lifecycle audit keyed by
  `(run_id, step_id, attempt, event_type)`. This is what makes **code** steps
  observable, since they emit no agent runtime events.

`WorkflowStore` ([`store.py`](./store.py)) is opened and shared by `AgentPool`.

## Service and API

`WorkflowService`:

- `list_workflows()` / `list_runs(workflow_id, ...)` / `get_run(workflow_id, run_id)`
  — step runs read from the store; strategy runs project from DBOS status.
- `resume_run(workflow_id, run_id)` — resume a failed step pipeline.
- `list_run_step_events(...)` — the step audit trail.
- `stream_run_events(...)` — SSE; store-backed for step pipelines (code steps
  visible), DBOS-status-polled for strategy workflows.

HTTP (via `mash.api`):

- `GET  /api/v1/workflow`
- `POST /api/v1/workflow/{workflow_id}/run`
- `GET  /api/v1/workflow/{workflow_id}/runs`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}`
- `POST /api/v1/workflow/{workflow_id}/runs/{run_id}/resume`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}/step-events`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}/events` (SSE)

## Dynamic publishing

`POST /api/v1/agent/{agent_id}/workflow` authors an agent-step workflow over the
wire (agent id + JSON-schema output + skill name). Code steps and pydantic
models are Python-only; the HTTP path is agent-step-only. Definitions are live
host state — republish on restart.

## Agent-step envelope

An agent step receives a JSON message:

```json
{
  "workflow_id": "changelog",
  "workflow_run_id": "mw:...:changelog:abc",
  "step_id": "summarize",
  "workflow_input": { ... },
  "input": { ...coerced step input == workflow_input merged with prior output... }
}
```

Each run is a clean slate — there is no cross-run state. The agent must return
structured output matching the step's `output` schema; that becomes the next
step's threaded input.

## CLI

- `/workflow list`
- `/workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]`
- `/workflow status <workflow_id> <run_id>`
- `/workflow resume <workflow_id> <run_id>`

# Workflows

`src/mash/workflows` is a DBOS-backed workflow layer on top of the Mash agent
runtime. A workflow is an ordered pipeline of typed steps with deterministic,
code-owned control flow; nondeterminism (an agent run, an external call) is
contained inside steps with schema-checked boundaries. Runs are durable (an
interrupted run resumes from its last checkpoint) and observable (a per-step
audit trail in a dedicated store).

Every workflow is a **step pipeline**: an ordered `steps` list run by the
forward-pipeline engine ([`engine.py`](./engine.py)). There is no alternative
execution shape. Dynamic control flow (fan-out over rows, conditional work)
lives inside a `CodeStep` body, guarded by stable replay identities.

## Steps

Each step is one of:

- **`CodeStep`** — deterministic Python: `run(inp, ctx) -> output`, sync or async.
  Pydantic-typed on both edges. Authored in Python only. The default body is a
  memoized DBOS step. An opt-in `orchestration=True` body instead runs in the
  parent workflow context so it can submit and await many agent requests,
  and is replayed after recovery, so it owns its own idempotency. Declare any
  statically known agent dependencies with `agent_ids=[...]`, independently of
  execution mode; registration validates them and tooling can discover them.
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

A pool built from workflows alone is a supported deploy shape:
`HostBuilder().workflow(spec).build()` with zero `.agent()` calls serves only
workflow runs. A `WorkflowSpec` of pure `CodeStep`s references no agents;
`AgentStep`s still require their `agent_id` to resolve in the pool. Hosts are
unrelated to this shape — workflow run endpoints are pool-level, and a `Host`
always names a primary agent.

## Idempotency

Step execution is at-least-once (DBOS recovery re-runs an interrupted step).
Pure transforms replay safely. A step with external effects must dedupe on a
stable key — `StepContext` carries `run_id`, `step_id`, `workflow_input`,
`attempt`, and `user_id`, all stable across retries. The framework never
invents a key or classifies steps.

## The run's user

A run acts for one user: `run_workflow(workflow_id, user_id=...)`, or
`user_id` next to `input` on `POST /workflow/{id}/run`. It is required when the
workflow submits agent requests, that is when it has an `AgentStep` or a
`CodeStep` that declares `agent_ids` (`WorkflowSpec.uses_agents`); otherwise
it is optional. The run stores it in `workflow_runs.user_id`, every agent step
submits for it, and a code step that submits its own agent requests passes
`ctx.user_id` to `post_inline_agent_request`. Those requests' code runs in that
user's sandbox. A resumed run keeps its user.

On a host with browsers, a code step drives the run user's browser with
`ctx.browser()`, an async context manager that yields a Playwright
`BrowserContext` over CDP (`pip install 'mashpy[browser]'`). It is the user's
profile, signed in wherever the user is, with no site list: workflow code is
the app's own. Pages the block opens close when it ends. A known flow can run
here and fall back to an `AgentStep` with browser tools when the page has
changed. An agent step that needs a sign-in fails at once, since nobody is
watching the run; an interactive request does the sign-in, and the next run
uses the session.

```python
async def fetch_bill(inp: BillIn, ctx: StepContext) -> BillOut:
    async with ctx.browser() as browser:
        page = await browser.new_page()
        await page.goto("https://www.pge.com/myaccount/billing")
        ...
```

An orchestration CodeStep is replayed as part of the parent workflow and is not
memoized as one opaque DBOS step. Use it only for dynamic orchestration, keep
child starts serial and deterministic, and record per-item progress under stable
identities in a durable ledger. Completed ledger items must be skipped when the
body is replayed. Terminal waits may run concurrently after their child
workflows have been started in deterministic order.

## Durability and resume

DBOS orchestrates and recovers runs. Each ordinary step body and each workflow
store write is its own memoized DBOS step, so a replay skips completed work;
store writes are idempotent so the crash-after-effect window converges rather
than duplicating. Orchestration CodeSteps recover from their application ledger.

- `resume_run(run_id)` — replay completed steps and re-drive from where the run
  stopped (same `run_id`). Agent steps interlock with their own durable request
  workflow through a deterministic `request_id`, so they resume mid-loop.
  Resume covers runs that stopped without finishing: a host that died mid-run,
  a cancelled run, one that exhausted its recovery attempts.
- `run_workflow(workflow_id, ...)` — a fresh `run_id` from step 1.

A **terminally failed run is not resumable** and `resume_run` rejects it with
`WorkflowResumeNotSupportedError` (HTTP 409). DBOS checkpoints a step's error
next to its output, so replaying a failed run re-raises the recorded error
instead of re-running the step, and `resume_workflows` skips rows already in
`SUCCESS` or `ERROR`. Reporting that no-op as a resume would be a lie; a failed
run starts over as a new run.

A step may declare `timeout_s`; exceeding it fails the step, not a retry. When
DBOS recovery attempts are exhausted the run is `failed`.

## Storage

Three tables (in the shared schema baseline `001_baseline.sql`, with
`workflow_runs.user_id` from `007_workflow_run_user.sql`), owned by the
workflow layer:

- `workflow_runs` — one row per run (status, `workflow_input`, `user_id`, `result`, timing).
- `workflow_steps` — one row per step (status, input/output snapshots, attempt,
  `agent_request_id`).
- `workflow_step_events` — append-only lifecycle audit keyed by
  `(run_id, step_id, attempt, event_type)`. This is what makes **code** steps
  observable, since they emit no agent runtime events.

`WorkflowStore` ([`store.py`](./store.py)) is opened and shared by `Pool`.

A run that finishes takes a `finished_seq` (`010_otel_export.sql`), and the
host's OTLP exporter, when one is configured, exports runs in that order: a
`mash.workflow <workflow_id>` root span with one `mash.workflow.step <step_id>`
span per step, and each agent step's request nested under its step. See
[`../otel/workflows.py`](../otel/workflows.py).

## Service and API

`WorkflowService`:

- `list_workflows()` / `list_runs(workflow_id, ...)` / `get_run(workflow_id, run_id)`
  — runs read from the store; a run not yet in the store (still queued)
  projects from DBOS status.
- `resume_run(workflow_id, run_id)` — re-drive an interrupted run; rejects a
  terminal one.
- `list_run_step_events(...)` — the step audit trail.
- `stream_run_events(...)` — SSE from the store, so code steps are visible.

HTTP (via `mash.api`):

- `GET  /api/v1/workflow`
- `GET  /api/v1/workflow/{workflow_id}`
- `POST /api/v1/workflow/{workflow_id}/run`
- `GET  /api/v1/workflow/{workflow_id}/runs`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}`
- `POST /api/v1/workflow/{workflow_id}/runs/{run_id}/resume`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}/step-events`
- `GET  /api/v1/workflow/{workflow_id}/runs/{run_id}/events` (SSE)

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

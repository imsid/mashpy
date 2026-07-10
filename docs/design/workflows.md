# Workflows — Design

Status: proposal
Author: sid (with Claude)
Supersedes: the current `src/mash/workflows` package

## Goal

A workflow guarantees the execution of a deterministic set of steps in a fixed
order. Each step is either:

- an **agent step** — one run of the Mash agent loop (non-deterministic), or
- a **code step** — a piece of deterministic Python.

Every step takes a `structured_input` and returns a `structured_output`. The
output of step *n* is persisted and passed forward as part of the input to step
*n+1*. The `structured_output` of the final step is the workflow result and is
persisted as such.

Workflows are durable — a run resumes from the failed step if it crashes
mid-execution — and observable, with a step-level audit trail that traces the
whole run including code steps.

## What changes from today

The current package (`spec.py`, `strategy.py`, `dbos.py`, `service.py`) is built
around a different model. The redesign changes four things at the core:

1. **Steps can be code, not only agents.** Today every `TaskSpec` binds to an
   agent and runs a Mash request. Running plain code requires writing a whole
   custom `WorkflowStrategy`. The workflow model makes a code step a first-class
   primitive.

2. **State threads forward within a run.** Today `load_previous_task_state`
   (`dbos.py:291`) deliberately *skips the current run* and pulls each task's
   state from the latest prior successful run — a cross-run per-task checkpoint
   model built for periodic jobs. The pipeline replaces this with pure forward
   state:
   step *n* sees `(workflow_input, output(n-1))`. `load_previous_task_state` and
   the cross-run lookup are deleted, not adapted.

3. **Typed I/O via pydantic.** Today `TaskSpec.structured_output` is a
   hand-written JSON-schema dict, validated on output only. Every step now has
   with pydantic models and validates at both edges. Pipeline adjacency is
   checked at build time.

4. **Workflows own persistence.** Today run history is reconstructed from agent
   memory turns and the runtime event log; DBOS holds status and output; there
   are no workflow tables. Code steps produce no agent turns, so they would be
   invisible under that model. Dedicated workflow tables hold and serve
   run history and events from them.

What stays: DBOS remains the orchestration and recovery engine. The agent loop
keeps its own durable request workflow. The `WorkflowStrategy` escape hatch stays
for non-linear shapes (fan-out, branching, eval scoring); the linear typed
pipeline is the default path.

## Authoring API

### Step specs

```python
from pydantic import BaseModel
from mash.workflows import AgentStep, CodeStep, WorkflowSpec, StepContext


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
    steps=[
        CodeStep(step_id="scan", run=scan, input=ScanIn, output=ScanOut),
        AgentStep(
            step_id="summarize",
            agent_id="writer",
            input=ScanOut,       # receives the prior step's output model
            output=SummaryOut,   # becomes the agent's structured-output schema
        ),
    ],
)
```

- An agent step's `output` model *is* the request's structured-output schema
  (derived with `.model_json_schema()`); the hand-written schema dicts go away.
- A step's `input` model is validated by coercing the incoming object
  (`workflow_input` merged with the prior output) into it before the step runs.
- Step 1 receives `(workflow_input, None)`. There is no prior output.
- The workflow result is `output(last)`, serialized with `model_dump(mode="json")`.

### Build-time validation

`WorkflowSpec` validates the pipeline when it is built, before any run:

- every `step_id` is unique and non-empty;
- each step's `input` model is satisfiable from the fields available at that
  point — `workflow_input` fields plus `output(n-1)` fields. A step that requires
  a field no upstream step produces is a build error, not a runtime failure;
- agent steps resolve to a registered agent id.

This catches broken pipelines at registration time.

### Idempotency is the author's job, not the framework's

The framework does not classify steps as pure or effectful. Every code step runs
inside a DBOS step and has its output memoized — that is forced by the
requirement that every `structured_output` is persisted — so there is no
behavioral fork for the framework to select on.

The one fact authors need to know is that step execution is **at-least-once**.
DBOS records a step's output after the body returns; if the process dies after
the body runs but before the output commits, recovery re-runs the body. For a
pure transform this is harmless (replay yields the same output). For a step with
external effects (DB write, HTTP call, payment) it can double-apply — so such a
step must dedupe on a stable key.

The framework's job is only to make a stable key *derivable*: `StepContext`
carries `run_id`, `step_id`, `workflow_input`, and `attempt`, all constant
across retries of the same logical step. The author builds or threads a key from
those; the framework does not invent one or gate behavior on a flag.

```python
class ChargeOut(BaseModel):
    charge_id: str

def charge(inp: InvoiceIn, ctx: StepContext) -> ChargeOut:
    # Author owns idempotency. A stable key is right here for the taking:
    key = f"{ctx.run_id}:{ctx.step_id}"          # or thread one via workflow_input
    return ChargeOut(charge_id=billing.charge(inp.amount, key=key))

CodeStep(step_id="charge", run=charge, input=InvoiceIn, output=ChargeOut)
```

## Execution model

The default engine runs steps in order. For each step:

1. Build `structured_input` = `workflow_input` merged with `output(n-1)`, coerced
   into the step's `input` model.
2. Run the step inside a DBOS step so its output is memoized:
   - **code step** — call `run(inp, ctx)`; validate the return against `output`.
   - **agent step** — post an agent request with a deterministic `request_id`
     (`uuid5(agent:run:step)`), forwarding `output` as the structured-output
     schema, and await the terminal payload.
3. Persist the step record and its output snapshot (see Storage).
4. `output(n)` becomes the input contribution for step *n+1*.

After the last step, persist `output(last)` as the run result.

### Resume vs. fresh run

Durability has two levels that interlock through the deterministic agent
`request_id`:

- **Workflow level.** On crash, DBOS recovery replays completed steps — their
  memoized outputs reconstruct the failed step's input identically — and
  re-drives from the failed step with the same input.
- **Agent level.** Because the re-posted agent request reuses the same
  `request_id`, it resumes the *same* agent request workflow rather than starting
  a new one. The agent loop continues from its own last durable point. If the
  step already reached its terminal payload, DBOS returns the memoized output and
  the agent does not re-run at all.

Two service operations expose the choice after a terminal failure. Note the
identifier: a run is resumed by its **`run_id`** (the run instance, which is the
DBOS workflow id), while a fresh run is started from the **`workflow_id`** (the
definition, e.g. `"changelog"`). The two must not collide in the signatures.

- `resume_run(run_id)` — same `run_id`; DBOS replays completed steps and resumes
  from the failed step with the same input. Agent steps do not re-run unless they
  never reached terminal.
- `run_workflow(workflow_id, ...)` — mints a new `run_id`; every step reruns from
  step 1, agent steps included, producing fresh (non-deterministic) outputs.

Automatic DBOS recovery already covers process-crash resume transparently. The
explicit `resume_run` is only for the human/caller decision after a run has
terminally failed.

## Storage

Three dedicated tables. Workflows stop reconstructing history from agent memory
turns.

### `workflow_runs`

| column | type | notes |
|---|---|---|
| `run_id` | text PK | DBOS workflow id |
| `workflow_id` | text | |
| `status` | text | queued / running / completed / failed / cancelled |
| `workflow_input` | jsonb | immutable trigger params |
| `result` | jsonb | `output(last)`, null until completed |
| `error` | text | null unless failed |
| `dedup_key` | text | nullable |
| `session_id` | text | caller session or per-run session |
| `created_at` / `started_at` / `finished_at` | double precision | epoch seconds, matching the rest of the Mash schema |

### `workflow_steps`

| column | type | notes |
|---|---|---|
| `run_id` | text | FK → workflow_runs |
| `workflow_id` | text | definition id; denormalized for querying steps across runs |
| `step_id` | text | |
| `ordinal` | int | position in the pipeline |
| `kind` | text | `agent` or `code` |
| `status` | text | pending / running / completed / failed |
| `input_snapshot` | jsonb | the coerced structured_input |
| `output_snapshot` | jsonb | the validated structured_output |
| `error` | text | |
| `attempt` | int | increments on recovery |
| `agent_request_id` | text | nullable; set for agent steps |
| `started_at` / `finished_at` | timestamptz | |

Primary key `(run_id, step_id)`.

### `workflow_step_events`

Append-only audit; this is what makes code steps observable, since they emit no
agent turns or runtime events.

| column | type | notes |
|---|---|---|
| `run_id` | text | |
| `workflow_id` | text | definition id; denormalized for querying events across runs |
| `step_id` | text | |
| `seq` | int | per-step monotonic |
| `event_type` | text | step.started / step.completed / step.failed / step.retried |
| `at` | double precision | epoch seconds |
| `payload` | jsonb | small; e.g. error message, attempt number |

Primary key `(run_id, step_id, attempt, event_type)` — the deterministic identity
of a lifecycle transition, so a re-run's re-append is a no-op. `seq` orders
events within a step for display.

### Consistency: DBOS-step-wrapped idempotent writes

Mash's store uses a psycopg pool that is separate from DBOS's own connection, so
a single shared transaction spanning the store write and DBOS's step-output
commit is not achievable across that boundary. Chasing literal atomicity there
would be the wrong target. Instead consistency rests on two mechanisms that
compose:

1. **Every store write is its own `run_step_async` step.** DBOS memoizes each
   step's completion, so on replay a store write that already finished is skipped
   entirely — no duplicate.
2. **The writes are idempotent on deterministic keys.** `workflow_steps` is an
   upsert keyed by `(run_id, step_id)`; `workflow_step_events` inserts with
   `ON CONFLICT (run_id, step_id, attempt, event_type) DO NOTHING`. This covers
   the one window (1) leaves — a crash after the side effect but before DBOS
   records the step — because the replay re-runs the body and the write converges
   instead of duplicating.

Together these keep the tables consistent with what DBOS will replay without a
cross-system transaction, and let `list_runs` / `get_run` / `stream_run_events`
read from the workflow tables instead of stitching together agent memory turns.

## Observability

- `list_runs(workflow_id, ...)` reads `workflow_runs` directly (status, timing,
  result), replacing today's reconstruction from `store.list_workflow_turns`.
- `get_run(run_id)` returns the run row plus its `workflow_steps` rows —
  per-step status, I/O snapshots, and agent request ids.
- `stream_run_events(run_id)` merges `workflow_step_events` (all steps, including
  code) with each agent step's own runtime event log (via its `agent_request_id`)
  for token-level trace. Code steps are visible through the step-event stream;
  agent steps additionally carry their loop trace.

## Migration

### `changelog`

The current `changelog` workflow relies on cross-run `last_run_ts`
checkpointing, which the pure pipeline drops. The watermark moves to the trigger
boundary: the caller (cron/scheduler) passes the last watermark in as
`workflow_input`, and reads the new one out of the run `result` to persist for
the next invocation. This is a behavior change for whoever schedules the
workflow and must be called out in its release notes.

### Eval scoring strategy

The eval-scoring path is a custom `WorkflowStrategy` that uses
`post_inline_agent_request` / `collect_terminal_payload`. The strategy escape
hatch stays, so this keeps working. It should be revisited to use the new step
store for its audit trail, but that is not a blocker for the cutover.

### Clean cutover — no compatibility shim

This is a hard cutover. `TaskSpec` and `WorkflowSpec(tasks=[...])` are removed
outright; there is no shim adapting the old cross-run semantics. Every existing
workflow is rewritten to `WorkflowSpec(steps=[...])` with `AgentStep`/`CodeStep`.
The only in-tree callers are `changelog` and the eval strategy, both handled
above, so there is no external surface to preserve.

## Level of effort

| Work | Size |
|---|---|
| Step specs (`AgentStep`/`CodeStep`), pydantic I/O, build-time validation | M |
| Forward-pipeline engine; delete `load_previous_task_state` | S |
| Code-step execution as a DBOS step (memoized output; `StepContext`) | S |
| `workflow_runs` / `workflow_steps` / `workflow_step_events` tables | L |
| Observability rework: store-backed `list_runs` / `get_run` / `stream_run_events` | L |
| API / CLI / serialization; migrate `changelog`, revisit eval strategy | M |
| Tests + docs | M |

Rough total: ~5–6 focused weeks. The engine and typing are ~1 week; the
persistence and observability decoupling are the majority and carry the
architectural risk, because today workflows own no storage and lean entirely on
the agent event log.

## Resolved decisions

- **`resume_run` is a first-class operation** on the HTTP API and the CLI/REPL,
  not in-process only. It takes a `run_id` and resumes the run from its failed
  step.
- **Retry cap surfaces as `failed`.** Automatic DBOS recovery attempts are capped
  per run; when the cap is hit, DBOS terminal status `MAX_RECOVERY_ATTEMPTS_EXCEEDED`
  maps to the workflow run status `failed` (as it does today).
- **Steps take an optional timeout.** A step may declare a timeout; if set and
  exceeded, the step is a **failure**, not a retry — the run terminates as failed
  and is resumable via `resume_run` like any other step failure.

```python
CodeStep(step_id="charge", run=charge, input=InvoiceIn, output=ChargeOut, timeout_s=30)
AgentStep(step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut, timeout_s=120)
```

## Phased implementation plan

Each phase is one branch and one PR, ordered so the tree stays green. New code
lands under new names alongside the old package; the old surfaces are deleted in
one dedicated cutover phase (Phase 6), never half-removed. A phase is done only
when its exit criteria hold and its tests pass.

### Phase 0 — Spec and build-time validation

No runtime, no DBOS, no DB. Pure types and validation, fully unit-testable.

- Add `StepSpec`, `AgentStep`, `CodeStep`, `StepContext` to `spec.py`.
- `WorkflowSpec` gains `steps: list[StepSpec]`; keep `strategy` as-is.
- Pydantic I/O: each step declares `input` / `output` models; agent steps derive
  the request structured-output schema from `output.model_json_schema()`.
- Build-time pipeline validation: unique non-empty `step_id`s; each step's
  `input` fields satisfiable from `workflow_input` ∪ `output(n-1)`; agent step
  agent ids resolvable.
- `timeout_s` field on both step kinds (carried, not yet enforced).

Exit: constructing a valid `WorkflowSpec(steps=[...])` succeeds; a pipeline with
an unsatisfiable step input or duplicate id raises at build time. Unit tests only.

### Phase 1 — Storage layer

Independent of the engine; testable against a Postgres instance.

- Create `workflow_runs`, `workflow_steps`, `workflow_step_events` (schemas above,
  all three carrying `workflow_id`). Add migrations.
- A `WorkflowStore` with: create/finish run, upsert step + snapshots, append step
  event, and the read queries (`get_run`, `list_runs`, `get_run_steps`,
  `list_step_events`).
- Expose an atomic write path usable from inside a DBOS step body (Phase 2 relies
  on this for consistency).

Exit: store CRUD + read queries pass integration tests against Postgres. No
engine wiring yet.

### Phase 2 — Forward-pipeline engine

The core execution change. Depends on Phases 0–1.

- Replace `SequentialTaskStrategy` with the forward-pipeline default strategy:
  step *n* input = `workflow_input` ∪ `output(n-1)`, coerced into the step's
  `input` model; validate output against the `output` model.
- Code-step execution as a DBOS step (memoized); `StepContext` populated with
  `run_id` / `step_id` / `workflow_input` / `attempt`.
- Agent-step execution via the deterministic `request_id` (reuse existing inline
  path); await terminal payload.
- Enforce `timeout_s`: exceed → step failure (run fails, resumable), not a retry.
- Persist step records, snapshots, and step events **inside the DBOS step body**,
  atomic with the memoized output. Persist `output(last)` as the run `result`.
- Delete `load_previous_task_state` and the cross-run lookup.

Exit: an end-to-end run of a code+agent pipeline persists per-step snapshots and
a final result; a mid-pipeline crash resumes from the failed step with identical
input. Integration tests with a real agent and a code step.

### Phase 3 — Service and observability

Depends on Phase 2. Rewrites `service.py` to be store-backed.

- `list_runs` / `get_run` read `workflow_runs` + `workflow_steps` directly, not
  agent memory turns.
- `stream_run_events` merges `workflow_step_events` (all steps) with each agent
  step's runtime event log via `agent_request_id`.
- `resume_run(run_id)` and `run_workflow(workflow_id, ...)` service operations.

Exit: run history, per-step detail, and a live event stream (including a code-only
run) all come from the workflow tables; `resume_run` resumes a failed run.

### Phase 4 — API and CLI

Depends on Phase 3. Thin wrappers over the service.

- HTTP routes: run, `resume_run`, list runs, get run (with steps), stream events.
- REPL: `/workflow run`, `/workflow resume <run_id>`, `/workflow status`,
  updated to the new run/step/result shapes.

Exit: a workflow can be run, streamed, inspected, and resumed end-to-end from the
CLI against a live host.

### Phase 5 — Migrate in-tree callers

Depends on Phase 4. Move the two real users onto the new surface.

- Rewrite `changelog` to `steps=[...]`; move the `last_run_ts` watermark to the
  trigger boundary (in via `workflow_input`, out via run `result`). Update its
  scheduler/release notes.
- Point the eval `ScoreEvalsStrategy` at the new step store for its audit trail
  (still a strategy; the escape hatch is unchanged).

Exit: both in-tree workflows run on the forward engine; eval scoring shows up in the
step audit tables.

### Phase 6 — Cutover and cleanup

Depends on Phase 5 — nothing else references the old surface.

- Delete `TaskSpec`, `WorkflowSpec(tasks=...)`, `SequentialTaskStrategy`, and the
  old agent-turn-derived run history.
- Update `mash.workflows.__init__` exports, the workflows `README.md`, and
  `CLAUDE.md` workflow examples to the `steps=[...]` API.

Exit: no reference to `tasks=[...]` or cross-run task state remains; full suite
green; docs describe only the supported workflow model.

### Sequencing notes

- Phases 0 and 1 are independent and can run in parallel.
- Phase 2 is the critical-path, highest-risk phase (durable execution +
  atomic audit writes); land it before building anything on top.
- Phases 3–4 are mechanical once 2 is solid.
- Phase 6 is intentionally last and isolated so the deletion is a clean, reviewable
  diff rather than noise spread across every other PR.

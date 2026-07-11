---
name: build-mash-workflow
description: Build a durable Mash workflow as a typed step pipeline of CodeSteps and AgentSteps.
---

# Build a Mash Workflow

You are helping a developer build a workflow with the **Mash** Python SDK. A
workflow is an ordered **step pipeline**: each step is a `CodeStep`
(deterministic Python) or an `AgentStep` (one run of an agent's loop). Runs are
durable (a failed run resumes from the failed step) and observable (a per-step
audit trail in the workflow store).

This skill covers defining, registering, and running workflows. For scaffolding
the agents that `AgentStep`s run, load `build-mash-agent`; for attaching
workflows to a host composition, load `build-mash-host`.

## Step 1: Define the Pipeline

Every step has a pydantic `input` and `output`. Step *n*'s output threads into
step *n+1*'s input, merged over the immutable `workflow_input`, and the final
step's output is the run result.

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
    ...  # deterministic Python, sync or async


workflow = WorkflowSpec(
    workflow_id="changelog",
    input_model=ScanIn,  # types workflow_input; enables strict build-time checks
    steps=[
        CodeStep(step_id="scan", run=scan, input=ScanIn, output=ScanOut),
        AgentStep(step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut),
    ],
)
```

`WorkflowSpec` validates the pipeline at build time: unique step ids, typed
I/O, and, when `input_model` is set, field-level adjacency between steps.

### CodeStep

`run` is `run(inp, ctx) -> output`, sync or async, pydantic-typed on both
edges. The body executes as a memoized DBOS step, so a recovered run skips it
if it already completed. Declare statically known agent dependencies with
`agent_ids=[...]` so registration can validate them. Set `orchestration=True`
only when the body must start durable child workflows; that mode replays with
the parent workflow and its external work must be guarded by a durable,
idempotent ledger.

### AgentStep

An `AgentStep` targets an agent already in the pool by `agent_id`, or carries
its own `agent_spec`, which the builder registers as a **workflow-only agent**:
a full runtime that executes workflow steps but is hidden from public agent
listings and can't be named in a host.

- `output` may be a pydantic model or a JSON-schema dict; either becomes the
  request's structured-output schema, and the validated payload is the next
  step's threaded input.
- `input` may be `None` (passthrough) for agents that read `workflow_input`
  directly.
- An optional `skill_name` tells the agent to load that skill before doing the
  step's work, which keeps long task instructions in skill markdown instead of
  the step definition.

The agent receives a JSON message carrying `workflow_id`, `workflow_run_id`,
`step_id`, `workflow_input`, and the coerced step `input`. Each run is a clean
slate; there is no cross-run state.

### Idempotency

Step execution is at-least-once: DBOS recovery re-runs an interrupted step.
Pure transforms replay safely. A step with external effects must dedupe on a
stable key — `StepContext` carries `run_id`, `step_id`, `workflow_input`, and
`attempt`, all stable across retries (e.g. `f"{ctx.run_id}:{ctx.step_id}"`).
The framework never invents a key.

A step may declare `timeout_s`; exceeding it fails the step, and the run stays
resumable.

### Non-linear shapes

Fan-out and branching supply a `WorkflowStrategy` instead of `steps`; the
strategy owns its own DBOS registration and run body. See
`src/mash/workflows/strategy.py`. Prefer step pipelines unless the shape
demands it.

## Step 2: Register the Workflow

```python
pool = (
    HostBuilder()
    .agent(WriterAgent(), metadata=AgentMetadata(...))
    .workflow(workflow)
    .build()
)
```

Registration validates that every `agent_id` a step names exists in the pool.
No other wiring is needed; the API routes and REPL commands below work as soon
as the pool serves.

## Step 3: Run, Resume, Inspect

Over the API:

```bash
# start a run (dedup_key optional: a second trigger while a run is active is rejected)
curl -X POST http://127.0.0.1:8000/api/v1/workflow/changelog/run \
  -H "Content-Type: application/json" \
  -d '{"input": {"repo_url": "https://github.com/acme/app"}}'
# -> {"run_id": "..."}

# inspect and resume
curl http://127.0.0.1:8000/api/v1/workflow/changelog/runs/{run_id}
curl -X POST http://127.0.0.1:8000/api/v1/workflow/changelog/runs/{run_id}/resume

# stream events (SSE): agent-step runtime events + code-step lifecycle events
curl http://127.0.0.1:8000/api/v1/workflow/changelog/runs/{run_id}/events
```

`GET /api/v1/workflow` lists workflows, `GET .../workflow/{id}/runs` lists
runs, and `GET .../runs/{run_id}/step-events` returns the per-step audit
trail.

Resume replays completed steps from their stored outputs and re-drives the
pipeline from the failed step under the same `run_id`. Agent steps interlock
with their own durable request through a deterministic `request_id`, so a
resumed agent step continues mid-loop rather than resubmitting.

From the REPL:

```bash
/workflow list
/workflow run changelog --input '{"repo_url": "https://github.com/acme/app"}'
/workflow status changelog <run_id>
/workflow resume changelog <run_id>
```

## Reference Documentation

- Workflows: `src/mash/workflows/README.md`
- Deep dive: `docs/posts/workflows-as-step-pipelines.md`
- Runtime & hosting: `src/mash/runtime/README.md`

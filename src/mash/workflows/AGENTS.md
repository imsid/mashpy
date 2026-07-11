# AGENTS Guide for `src/mash/workflows`

## Scope
Durable, observable host-level workflows: ordered step pipelines orchestrated
by DBOS on top of the Mash agent runtime. Dynamic fan-out may be ordinary
Python inside a `CodeStep` when its external effects use stable replay
identities.

## What Must Stay True
- A `WorkflowSpec` supplies `steps` (a forward pipeline); it is the only
  execution shape. Step pipelines validate themselves at build time.
- Steps are `CodeStep` (deterministic Python, pydantic-typed) or `AgentStep`
  (one agent-loop run; pydantic-or-JSON-schema output, optional passthrough
  input and `skill_name`).
- Code that invokes agents declares the statically known dependencies through
  `CodeStep.agent_ids`, regardless of whether the step is orchestration code.
  Pool registration rejects unresolved declarations.
- State threads forward: step *n*'s output merges over `workflow_input` into step
  *n+1*'s input; the final step's output is the run result.
- The workflow layer owns its persistence — `workflow_runs`, `workflow_steps`,
  `workflow_step_events` via `WorkflowStore`. Run history and step audit come
  from the store, not agent memory turns.
- Each ordinary step body and store write is its own memoized DBOS step; store
  writes are idempotent (deterministic keys) so at-least-once replay converges.
- A `CodeStep(orchestration=True)` runs in the parent workflow context and may
  start child workflows. It must use a durable per-item ledger, skip completed
  items on replay, and start children in a serial deterministic order; only
  terminal collection may fan out concurrently.
- Idempotency of a step's external effects is the author's job (`StepContext`
  exposes stable `run_id`/`step_id`); the framework never invents keys.
- `AgentPool` owns workflow registration, the shared `WorkflowStore`, and
  `WorkflowService` construction.

## Change Rules
- Keep this package focused on orchestration, registration, storage, and DBOS
  contracts. No transport clients here.
- Workflows are defined in Python at build time (pydantic models + callables);
  there is no over-the-wire workflow registration.
- If the agent-step envelope or output contract changes, update this package,
  the masher/pilot workflow agents that consume it, the README, and the tests
  together.
- If the store schema changes, update the migration, `store.py`, `WorkflowService`
  reads, and tests together.
- If registration semantics change, update `HostBuilder`/`AgentPool` tests together.

## Minimal Validation
- `python -m compileall src/mash/workflows`
- Verify a code+agent step pipeline: output threading, result persistence, a
  failed step marking the run failed with a `step.failed` audit event
  (`tests/mash/workflows/test_forward_engine.py`).
- Verify store-backed service reads and `resume_run`
  (`tests/mash/workflows/test_service_store.py`).
- Verify API paths for `GET /workflow`, `GET /workflow/{id}`,
  `POST /workflow/{id}/run`,
  `GET /workflow/{id}/runs/{run_id}`, `.../resume`, `.../step-events`,
  `.../events`, and not-found/conflict errors.

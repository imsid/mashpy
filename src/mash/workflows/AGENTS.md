# AGENTS Guide for `src/mash/workflows`

## Scope
Durable, observable host-level workflows: ordered step pipelines (or a
`WorkflowStrategy` for non-linear shapes) orchestrated by DBOS on top of the Mash
agent runtime.

## What Must Stay True
- A `WorkflowSpec` supplies `steps` (a forward pipeline) or a `strategy` — one of
  the two. Step pipelines validate themselves at build time.
- Steps are `CodeStep` (deterministic Python, pydantic-typed) or `AgentStep`
  (one agent-loop run; pydantic-or-JSON-schema output, optional passthrough
  input and `skill_name`).
- State threads forward: step *n*'s output merges over `workflow_input` into step
  *n+1*'s input; the final step's output is the run result.
- The workflow layer owns its persistence — `workflow_runs`, `workflow_steps`,
  `workflow_step_events` via `WorkflowStore`. Run history and step audit come
  from the store, not agent memory turns.
- Each step body and store write is its own memoized DBOS step; store writes are
  idempotent (deterministic keys) so at-least-once replay converges.
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

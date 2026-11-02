# AGENTS Guide for `src/mash/workflows`

## Scope
Durable, observable host-level workflows: ordered step pipelines run by the
host on top of the Mash agent runtime. Dynamic fan-out may be ordinary Python
inside a `CodeStep` when its external effects use stable identities.

## What Must Stay True
- A `WorkflowSpec` supplies `steps` (a forward pipeline); it is the only
  execution shape. Step pipelines validate themselves at build time.
- Steps are `CodeStep` (deterministic Python, pydantic-typed) or `AgentStep`
  (one agent-loop run; pydantic-or-JSON-schema output, optional passthrough
  input and `skill_name`).
- Code that invokes agents declares the statically known dependencies through
  `CodeStep.agent_ids`. Pool registration rejects unresolved declarations.
- State threads forward: step *n*'s output merges over `workflow_input` into step
  *n+1*'s input; the final step's output is the run result.
- The workflow layer owns its persistence — `workflow_runs`, `workflow_steps`,
  `workflow_step_events` via `WorkflowStore`. Run history and step audit come
  from the store, not agent memory turns.
- A run's durable state is its store rows: a run continues by skipping its
  `completed` steps and threading their stored outputs. Store writes are
  idempotent (deterministic keys) so at-least-once step execution converges.
- A step's `attempt` stays the same when the host died in it and goes up when
  a failed step is resumed; agent request ids include it.
- A code step that fans out over agent requests keeps a durable per-item
  ledger and skips completed items when it runs again.
- Idempotency of a step's external effects is the author's job (`StepContext`
  exposes stable `run_id`/`step_id`); the framework never invents keys.
- `Pool` owns workflow registration, the shared `WorkflowStore`, and
  `WorkflowService` construction.

## Change Rules
- Keep this package focused on execution, registration, and storage. No
  transport clients here.
- Workflows are defined in Python at build time (pydantic models + callables);
  there is no over-the-wire workflow registration.
- If the agent-step envelope or output contract changes, update this package,
  the masher/pilot workflow agents that consume it, the README, and the tests
  together.
- If the store schema changes, update the migration, `store.py`, `WorkflowService`
  reads, and tests together.
- If registration semantics change, update `HostBuilder`/`Pool` tests together.

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

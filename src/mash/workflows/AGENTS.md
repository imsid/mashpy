# AGENTS Guide for `src/mash/workflows`

## Scope
Code-defined host-level workflows that orchestrate existing Mash agents.

## What Must Stay True
- `WorkflowSpec` remains a code-defined ordered list of `TaskSpec` items.
- `TaskSpec` stays minimal: `task_id` identifies workflow state, `agent_spec` identifies execution.
- `WorkflowService` orchestrates workflows through normal Mash agent requests; it does not bypass the runtime.
- Task input/output stays JSON-over-message text in this phase.
- DBOS owns workflow orchestration, run status, run history, and active-run deduplication.
- Task state is derived from the latest successful DBOS workflow output for the same `task_id`.
- Workflow logic does not own artifacts; agents handle their own file or tool side effects.
- `AgentHost` owns workflow registration and workflow service construction.

## Change Rules
- Keep this package focused on workflow orchestration, registration, and DBOS workflow contracts.
- Do not add transport clients here.
- Do not move task state into agent memory/session history unless the workflow contract is explicitly redesigned.
- If workflow request or output JSON shape changes, update this package README and the API tests together.
- If workflow registration semantics change, update `HostBuilder`/`AgentHost` tests together.
- If DBOS workflow status/output shape changes, update `WorkflowService`, API serialization, and tests together.

## Minimal Validation
- `python -m compileall src/mash/workflows`
- Verify one successful single-task workflow, one multi-task workflow, one duplicate-dedup rejection, and one invalid-JSON failure path.
- Verify API behavior for `GET /workflow`, successful `POST /workflow/{id}/run`, `GET /workflow/{id}/runs/{run_id}`, and not-found/conflict error paths.

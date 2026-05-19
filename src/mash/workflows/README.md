# Workflows

`src/mash/workflows` adds a DBOS-backed host-level workflow layer on top of the
existing Mash agent request runtime.

This package is for code-defined workflows only. A workflow is an ordered list of
tasks, and each task delegates execution to a registered Mash agent.

The current design is intentionally small:

- workflows are registered in Python, not created over the API
- tasks carry the `AgentSpec` that executes them
- DBOS owns workflow orchestration, run history, status, and active-run deduplication
- task state is derived from prior successful DBOS workflow outputs
- the workflow framework does not own artifacts
- each task execution still runs through the normal Mash request path

## Public Surface

Import the workflow API from `mash.workflows`:

- `TaskSpec`
- `WorkflowSpec`
- `WorkflowRegistry`
- `WorkflowRun`
- `WorkflowService`

## How To Define A Workflow

Define a workflow with one or more `TaskSpec` objects:

```python
from mash.workflows import TaskSpec, WorkflowSpec


CHANGELOG_WORKFLOW = WorkflowSpec(
    workflow_id="changelog",
    tasks=[
        TaskSpec(
            task_id="scan-codebase-and-append-changelog",
            agent_spec=changelog_agent_spec,
        ),
    ],
)
```

`task_id` identifies the workflow node and its persisted task state.

`agent_spec` is registered by `HostBuilder.workflow(...)` as a workflow-only
agent unless the same spec is already registered as the primary agent or a
subagent.

## How To Register A Workflow

Register workflows through `HostBuilder`, alongside the agents they depend on:

```python
from mash.runtime import HostBuilder
from mash.workflows import TaskSpec, WorkflowSpec


builder = (
    HostBuilder()
    .primary(primary_spec)
    .workflow(
        WorkflowSpec(
            workflow_id="changelog",
            tasks=[
                TaskSpec(
                    task_id="scan-codebase-and-append-changelog",
                    agent_spec=changelog_agent_spec,
                )
            ],
        )
    )
)

host = builder.build()
```

The host exposes a `WorkflowRegistry` and `WorkflowService`.

## DBOS Orchestration

`WorkflowService.run_workflow(...)` starts a DBOS workflow named
`mash.workflow.execute` and returns after the run is queued or started. The
returned `WorkflowRun` is a projection of DBOS workflow status.

When a `dedup_key` is provided, the workflow is enqueued with a DBOS queue
deduplication id. Active duplicate runs are rejected. Once DBOS marks the queued
workflow complete, the deduplication id is released by DBOS.

Task state is append-only. Before a task runs, Mash looks at recent successful
DBOS workflow outputs for the same workflow and passes the latest
`task_states[task_id]` object to the agent. If no prior state exists, the
framework passes `{}`.

Callers may also pass a per-run workflow input object. Workflow input is immutable
for the run, is passed to every task request, and is separate from task state.
Use workflow input for trigger parameters. Use task state only for checkpoints.

## Task Execution Contract

`WorkflowService` sends a normal Mash request to the target agent. The request
message is JSON text with this shape:

```json
{
  "workflow_id": "changelog",
  "workflow_run_id": "mw:h_example:changelog:abc",
  "task_id": "scan-codebase-and-append-changelog",
  "workflow_input": {
    "target_agent_id": "primary"
  },
  "task_state": {
    "last_run_ts": "2026-05-14T00:00:00Z"
  }
}
```

The target agent must return JSON text only, and that JSON must decode to an
object. That object becomes the next task state in the DBOS workflow output.

Example task output:

```json
{
  "last_run_ts": "2026-05-14T00:15:00Z"
}
```

If the agent returns invalid JSON, non-object JSON, or a failed request, the DBOS
workflow run fails and the last successful task state remains the latest state.

## How To Run Workflows

Inside the host process, call `WorkflowService` directly:

```python
workflow_service = host.get_workflow_service()
workflows = await workflow_service.list_workflows()
run = await workflow_service.run_workflow(
    "changelog",
    dedup_key="manual-2026-05-14",
    workflow_input={"target_agent_id": "primary"},
)
latest = await workflow_service.get_run("changelog", run.run_id)
```

## HTTP API

When the host is wrapped by `mash.api`, workflows are exposed through:

- `GET /api/v1/workflows`
- `POST /api/v1/workflows/{workflow_id}/run`
- `GET /api/v1/workflows/{workflow_id}/runs/{run_id}`

## CLI

The interactive Mash REPL exposes workflow commands as thin wrappers around the
HTTP API:

- `/workflow list`: list registered workflows
- `/workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]`: start a workflow run
- `/workflow status <workflow_id> <run_id>`: show workflow run status

Workflow CLI commands start runs asynchronously and print the run id/status. They
do not poll until completion. When a completed run has DBOS output, status
responses include that output.

## What This Package Does Not Do

- It does not define a standalone workflow client.
- It does not add top-level `mash workflow ...` commands.
- It does not define workflow table schemas or a default workflow store backend.
- It does not own artifacts or file outputs produced by agents.

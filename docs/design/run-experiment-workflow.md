# Run Experiment Workflow

## Goal

Replace the custom eval-scoring `WorkflowStrategy` with a normal, store-backed
workflow. Fan-out remains ordinary async Python inside `CodeStep` bodies, while
agent requests retain deterministic workflow/request identities.

The public workflow is `run-experiment`:

```text
prepare-experiment (code) -> execute-rows (code) -> judge-rows (code)
```

Its input is `{eval_id, host_id}`. The first step snapshots the target host,
agent specs, rubric, and dataset into an experiment plus one durable row record
per dataset row. The next two steps use those records as a replay-safe work
ledger. Those fan-out phases are `CodeStep(orchestration=True)` bodies: they run
in the parent workflow context because DBOS cannot start a child workflow from
inside a memoized step body.
The `judge-rows` step independently declares
`agent_ids=["eval-judge-agent"]`, making that code-level dependency visible to
registration checks and deployment tooling.

## Invariants

- Experiment and row identities are deterministic from the workflow run and
  dataset row identities.
- The target host is always invoked with the experiment's stored host snapshot.
- A row moves monotonically through `pending -> executing -> executed ->
  judging -> scored`, with `execution_failed` and `scoring_failed` as terminal
  row outcomes.
- A failed row does not fail the fan-out step; infrastructure failures that
  prevent the phase from making progress do fail the workflow step.
- Host and judge requests use deterministic task ids, so replay rejoins an
  existing request instead of duplicating model work.
- Child workflows are started serially in stable row order. Once a batch has
  started, terminal collection and row persistence run concurrently.
- The first unfinished row in each agent phase runs alone before the remaining
  fan-out to warm the provider prompt cache.
- Agent scoring is returned as structured output, but criterion validation and
  weighted-score arithmetic remain deterministic Python.

## Phases

### Phase 1: durable experiment ledger

- Add experiment workflow identity, target host, and rubric snapshot fields.
- Add row ordinal and lifecycle status fields.
- Add an atomic create-experiment-with-rows store operation.
- Keep existing eval read/comparison APIs compatible.

Exit: an experiment and all of its pending row records are created atomically
and can be reloaded by lifecycle status.

### Phase 2: dedicated judge agent

- Split judging from `EvalAgentSpec` into `EvalJudgeAgentSpec`.
- Register both built-in agents in every pool.
- Keep the judge tool-less, skill-less, and structured-output-only.

Exit: the pool exposes a generation agent and a separate judging agent.

### Phase 3: normal workflow migration

- Implement typed prepare, execute, and judge code steps.
- Preserve host snapshots, per-row sessions, metrics, partial failures,
  concurrency, and cache warm-up.
- Register `run-experiment` as a step pipeline.
- Delete the legacy custom strategy and its DBOS queue/child workflow registration.

Exit: experiment execution has workflow-store history and no custom strategy.

### Phase 4: product surfaces and documentation

- Point the Evals admin flow at `run-experiment` with `{eval_id, host_id}`.
- Update public exports, tests, module documentation, and workflow descriptions.

Exit: no in-tree caller or documentation references the legacy workflow ID.

### Phase 5: verification

- Unit-test lifecycle transitions, idempotent preparation, row failures,
  deterministic requests, cache warm-up, scoring, and aggregation.
- Run workflow, Masher, eval, API, runtime integration, lint, and compile checks.

Exit: the focused and repository-level validation suites are green.

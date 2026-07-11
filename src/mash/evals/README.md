# Evals

`src/mash/evals` holds the synthetic evals subsystem: the data model, the
read/write service, per-row operational metrics, and the Postgres store. It
answers the cold-start problem — evaluating a host before it has any real
traffic — by generating a dataset and rubric from the host's declared
capabilities, then scoring experiment runs against them.

The package is storage and computation only. Generation and scoring live in
the masher workflows (`src/mash/agents/masher`); the HTTP surface lives in
`src/mash/api/routes/evals.py`; the admin UI surface is the Evals tab
(`src/mash/api/web-admin`). See `docs/posts/synthetic-evals.md` for the design
narrative.

## Data model (`models.py`)

- `Eval` — one evaluation setup bound to a `host_id`: the developer's
  `user_guidance` plus a `dataset_id` and `rubric_id`.
- `DatasetRow` — one test case: `input`, `scenario_description`,
  `sampling_category`, `expected_behavior`.
- `ScoringRubric` / `ScoringCriterion` — a `global_scoring_prompt` and
  weighted criteria, each with its own `scoring_prompt` and a 1–5 scale by
  default.
- `Experiment` — one full run of the dataset against the host. Captures
  `host_composition`, `agent_spec_snapshot`, and `rubric_snapshot` at run start.
- `ExperimentRun` — one row's result: `actual_output`, `weighted_score`,
  per-criterion `CriterionScore`s with rationales, the `session_id` it
  executed under, lifecycle `status`, `error`, and operational `metrics`.

## Lifecycle

1. **Generate** — the `gen-synthetic-evals` masher workflow takes a host id,
   optional user guidance, and a row count; it derives a dataset and rubric
   from the host's composition and persists them via
   `EvalService.persist_eval`.
2. **Refine** — the rubric can be edited (`update_rubric`) until the eval has
   an experiment. From then on the eval is locked (`is_eval_locked` — derived,
   not stored) so scores stay comparable across experiments; measuring
   something different means generating a new eval.
3. **Run** — the `run-experiment` workflow prepares a durable row ledger,
   executes unfinished rows through the snapshotted host, then judges executed
   rows with `eval-judge-agent`. All three phases are ordinary `CodeStep`s.
4. **Read and compare** — `get_experiment_summary` aggregates mean and
   per-criterion scores plus operational rollups; `compare_experiments` pairs
   two experiments row-by-row and includes the agent-spec diff
   (`diff_agent_specs`) between their snapshots.

## Operational metrics (`metrics.py`)

Every row runs under a single `session_id` shared by the primary and its
subagents, so the runtime events for that session are a self-contained record
of what the run cost. `compute_row_metrics` is a pure fold over those events
(no I/O): latency, steps, LLM calls, tool calls (with per-tool breakdown),
tokens including cache read/creation, and recursive subagent metrics. The
scoring workflow calls it once per row; quality (rubric score) and cost
(metrics) stay independent.

## Storage (`postgres/`)

`PostgresEvalStore` persists to dedicated tables — `eval`, `eval_dataset`,
`eval_dataset_row`, `eval_rubric`, `eval_experiment`, `eval_experiment_run` —
in the same database as the runtime store (`MASH_DATABASE_URL`). The API app
wires it in its lifespan: when a database URL is configured it opens the
store, builds an `EvalService`, and binds it to masher's runtime context;
without one the `/evals` routes return `503 EVALS_NOT_AVAILABLE`.

## Public surface

Import from `mash.evals`:

- `EvalService`, `PostgresEvalStore`
- `Eval`, `DatasetRow`, `ScoringRubric`, `ScoringCriterion`, `Experiment`,
  `ExperimentRun`, `CriterionScore`
- `diff_agent_specs`
- `EvalNotFoundError`, `EvalLockedError`, `ExperimentNotFoundError`

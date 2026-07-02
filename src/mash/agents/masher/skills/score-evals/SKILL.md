---
name: score-evals
description: Run a synthetic eval dataset against the current host and score each output.
---

# Score Evals

Use this skill only for workflow id `score-evals` and task id `score-evals`.

Purpose: Load an existing eval, submit each dataset row to the host, score the outputs with an LLM judge against the rubric, and persist the results as an experiment.

Workflow contract:
1. Parse the request JSON. Read `workflow_input` exactly as provided.
2. Extract `eval_id` (required) from `workflow_input`.
3. Call `run_score_evals_workflow` with the exact `workflow_input`.
4. Use the tool result as the workflow outcome.

Required output shape:
- `experiment_id`: the persisted experiment identifier
- `eval_id`: the eval this experiment ran against
- `status`: `"completed"` or `"failed"`
- `scored_count`: number of rows successfully scored
- `mean_score`: weighted mean score across all scored rows (null if none scored)

Scoring rules:
- Submit rows to the host in parallel, bounded by `max_parallel_tools`.
- Score each (input, actual_output) pair against every rubric criterion using the criterion's `scoring_prompt` and the rubric's `global_scoring_prompt`.
- Score on the criterion's scale (default 1–5).
- Compute `weighted_score` per row as `sum(criterion.weight * score)` across all criteria.
- Capture `rationale` for each criterion score.
- Capture the current AgentSpec snapshot and compute delta from the eval's baseline before persisting.

If the tool returns an error, return an object with status `"failed"` and `error`.

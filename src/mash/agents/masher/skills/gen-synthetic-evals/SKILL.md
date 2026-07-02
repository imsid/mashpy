---
name: gen-synthetic-evals
description: Generate a synthetic eval dataset and scoring rubric for a host composition.
---

# Gen Synthetic Evals

Use this skill only for workflow id `gen-synthetic-evals` and task id `generate-evals`.

Purpose: Generate up to 100 synthetic test-case rows and a weighted scoring rubric from a host's declared capabilities, then persist them as an eval.

Workflow contract:
1. Parse the request JSON. Read `workflow_input` exactly as provided.
2. Extract `host_id` (required) and `user_guidance` (optional) from `workflow_input`.
3. Call `run_gen_synthetic_evals_workflow` with the exact `workflow_input`.
4. Use the tool result as the workflow outcome.

Required output shape:
- `eval_id`: the persisted eval identifier
- `host_id`: the host this eval covers
- `dataset_id`: the generated dataset identifier
- `rubric_id`: the generated rubric identifier
- `row_count`: number of dataset rows generated

Sampling categories for dataset rows:
- `random`: general-purpose inputs
- `multi_tool`: inputs requiring multiple tool calls
- `multi_agent`: inputs requiring subagent delegation
- `high_tokens`: inputs likely to produce long outputs
- `long_running`: inputs requiring many agentic steps
- `short_running`: inputs resolved in one or two steps

Rubric criteria must have weights summing to 1.0. Derive criteria from the host's declared capabilities in its agent metadata.

If the tool returns an error, return an object with status `"failed"` and `error`.

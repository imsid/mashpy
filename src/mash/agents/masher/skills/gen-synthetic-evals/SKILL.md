---
name: gen-synthetic-evals
description: Generate a synthetic eval dataset and scoring rubric for a host composition.
---

# Gen Synthetic Evals

Use this skill only for workflow id `gen-synthetic-evals` and task id `generate-evals`.

Purpose: Generate synthetic test-case rows and a weighted scoring rubric from a host's declared capabilities, then persist them as an eval.

Workflow contract:
1. Parse the request JSON. Read `workflow_input` exactly as provided.
2. Extract `host_id` (required), `user_guidance` (optional), and `row_count`
   (optional; default 20, max 100) from `workflow_input`.
3. Generate exactly `row_count` dataset rows and a scoring rubric from the
   host's declared capabilities (see the row/rubric shapes below). You do the
   generation — the tool only persists, and it rejects a dataset whose size
   does not match `row_count`.
4. Call `run_gen_synthetic_evals_workflow` with `host_id`, `user_guidance`,
   `row_count`, your generated `dataset_rows`, and `rubric`.
5. Use the tool result as the workflow outcome.

Each dataset row must have: `input`, `scenario_description`, `sampling_category`
(one of the categories below), `expected_behavior`, and `target_agents` (array of
agent ids). The rubric must have `global_scoring_prompt` and `criteria`, where each
criterion has `name`, `description`, `weight`, `scoring_prompt`, and optional
`scale_min`/`scale_max`.

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

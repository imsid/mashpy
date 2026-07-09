---
name: gen-synthetic-evals
description: Generate a synthetic eval dataset and scoring rubric for a host composition.
---

# Gen Synthetic Evals

Use this skill only for workflow id `gen-synthetic-evals` and step id `generate`.

Purpose: generate synthetic test-case rows and a weighted scoring rubric for
the host composition described in the request. You only generate — the
workflow validates and persists your structured output in a later code step.
Do not call any workflow tool.

The request's `input` object is the generation brief:

- `host_id`, `user_guidance`, `row_count` — the trigger parameters.
- `primary_agent_id` and `agent_profiles` — the host's actual composition;
  each profile carries the agent's declared `capabilities`, `description`,
  and `usage_guidance`.

Generate exactly `row_count` dataset rows and one rubric, then return them as
your structured output: `{"dataset_rows": [...], "rubric": {...}}`.

Each dataset row must have: `input`, `scenario_description`, `sampling_category`
(one of the categories below), `expected_behavior`, and `target_agents` (array of
agent ids drawn from `agent_profiles`). The rubric must have
`global_scoring_prompt` and `criteria`, where each criterion has `name`,
`description`, `weight`, `scoring_prompt`, and optional `scale_min`/`scale_max`.

Sampling categories for dataset rows:

- `random`: general-purpose inputs
- `multi_tool`: inputs requiring multiple tool calls
- `multi_agent`: inputs requiring subagent delegation
- `high_tokens`: inputs likely to produce long outputs
- `long_running`: inputs requiring many agentic steps
- `short_running`: inputs resolved in one or two steps

Rubric criteria weights must sum to 1.0. Derive criteria from the capabilities
declared in `agent_profiles`, and honor `user_guidance` when it narrows scope
or emphasis.

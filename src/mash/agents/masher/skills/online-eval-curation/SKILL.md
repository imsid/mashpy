---
name: online-eval-curation
description: Build normalized online eval JSONL rows from Mash log files.
---

# Online Eval Curation

Use this skill only for workflow id `masher-online-eval-curation` and step id `curate-online-evals`.

Purpose:
- Build normalized online eval JSONL rows from Mash runtime trace events.
- Curate dataset examples only; do not judge or score output quality.
- In trace and batch mode, append eval rows to Masher's configured online eval JSONL artifact.

Workflow contract:
1. Parse the request JSON.
2. Read `workflow_input` exactly as provided.
3. Call `run_online_eval_curation_workflow` with the exact `workflow_input`.
4. Use the tool result as the workflow outcome.

Required output shape:
- `schema_version`
- `target_agent_id`
- `session_id`
- `trace_id`
- `user_message`
- `assistant_response`
- `tools_called`
- `tool_call_count`
- `step_count`
- `input_tokens`
- `output_tokens`

Input modes:
- `trace`: requires `target_agent_id`, `session_id`, and `trace_id`; appends one eval row and returns the appended record.
- `batch`: requires `target_agent_id`; processes all of the target's traces and appends eval rows, returning counts. Optional `since_ts` (a unix timestamp) limits to traces after it; `limit` caps how many are scanned. Each run is a clean slate — no checkpoint is read or written.

Rules:
- Keep records machine-friendly and compact.
- Use raw event data for metrics whenever possible.
- Do not include trace digest narrative fields such as `summary`, `metrics`, or `notable_events`.
- If the tool returns an error, return an object with `schema_version`, status `"failed"`, and `error`.

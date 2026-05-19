---
name: online-eval-curation
description: Build normalized online eval JSONL rows from Mash log files.
---

# Online Eval Curation

Use this skill only for workflow id `{{workflow_id}}` and task id `{{task_id}}`.

Purpose:
- Build normalized online eval JSONL rows from Mash runtime trace events.
- Curate dataset examples only; do not judge or score output quality.
- In trace and incremental mode, append eval rows to `{{artifact_path}}`.

Workflow contract:
1. Parse the request JSON.
2. Read `workflow_input` and `task_state` exactly as provided.
3. Call `{{tool_name}}` with the exact `workflow_input` and `task_state`.
4. Return the tool result JSON text exactly and nothing else.

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
- `incremental`: requires `target_agent_id`; uses `task_state.checkpoints[target_agent_id].last_run_ts`, appends new eval rows, and returns checkpoint state.

Rules:
- Keep records machine-friendly and compact.
- Use raw event data for metrics whenever possible.
- Do not include trace digest narrative fields such as `summary`, `metrics`, or `notable_events`.
- If the tool returns an error, return a JSON object with `schema_version`, status `"failed"`, and `error`.

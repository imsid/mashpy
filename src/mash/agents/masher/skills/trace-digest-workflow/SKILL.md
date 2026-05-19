---
name: trace-digest-workflow
description: Run Masher's diagnostic trace digest workflow.
---

# Trace Digest Workflow

Use this skill only for workflow id `masher-trace-digest` and task id `digest-traces`.

Purpose:
- Explain and debug agent execution traces.
- Summarize status, metrics, and notable failure/error events.
- In incremental mode, append diagnostic digest records to Masher's configured trace digest JSONL artifact.

Workflow contract:
1. Parse the request JSON.
2. Read `workflow_input` and `task_state` exactly as provided.
3. Call `run_trace_digest_workflow` with the exact `workflow_input` and `task_state`.
4. Return the tool result JSON text exactly and nothing else.

Input modes:
- `trace`: requires `target_agent_id`, `session_id`, and `trace_id`; returns one digest and does not write the JSONL artifact.
- `incremental`: requires `target_agent_id`; uses `task_state.checkpoints[target_agent_id].last_run_ts`, appends digest records, and returns checkpoint state.

Rules:
- Treat `task_state` only as checkpoint data.
- Do not produce online eval dataset rows from this workflow.
- If the tool returns an error, return a JSON object with `schema_version`, status `"failed"`, and `error`.

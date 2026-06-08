---
name: trace-digest-workflow
description: Run Masher's diagnostic trace digest workflow with latency analysis.
---

# Trace Digest Workflow

Use this skill only for workflow id `masher-trace-digest` and task id `digest-traces`.

Purpose:
- Produce deterministic latency analysis and diagnostics for agent execution traces.
- Break down time spent in LLM inference, tool execution, cold start, context loading, and subagent calls.
- Stitch child subagent traces for nested latency breakdowns (up to 3 levels deep).
- Summarize status, metrics, and notable failure/error events.
- In incremental mode, append diagnostic digest records (schema v2) to Masher's configured trace digest JSONL artifact.

Workflow contract:
1. Parse the request JSON.
2. Read `workflow_input` and `task_state` exactly as provided.
3. Call `run_trace_digest_workflow` with the exact `workflow_input` and `task_state`.
4. Use the tool result as the workflow outcome.

Input modes:
- `trace`: requires `target_agent_id`, `session_id`, and `trace_id`; returns one digest and does not write the JSONL artifact.
- `incremental`: requires `target_agent_id`; uses `task_state.checkpoints[target_agent_id].last_run_ts`, appends digest records, and returns checkpoint state.

All latency stats are computed deterministically from runtime_event_log data. No model inference is used for metrics computation.

Rules:
- Treat `task_state` only as checkpoint data.
- Do not produce online eval dataset rows from this workflow.
- If the tool returns an error, return an object with `schema_version`, status `"failed"`, and `error`.

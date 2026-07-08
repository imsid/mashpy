---
name: trace-digest-workflow
description: Run Masher's diagnostic trace digest workflow with latency analysis.
---

# Trace Digest Workflow

Use this skill only for workflow id `masher-trace-digest` and step id `digest-traces`.

Purpose:
- Produce deterministic latency analysis and diagnostics for agent execution traces.
- Break down time spent in LLM inference, tool execution, cold start, context loading, and subagent calls.
- Stitch child subagent traces for nested latency breakdowns (up to 3 levels deep).
- Summarize status, metrics, and notable failure/error events.
- In batch mode, append diagnostic digest records to Masher's configured trace digest JSONL artifact.

Workflow contract:
1. Parse the request JSON.
2. Read `workflow_input` exactly as provided.
3. Call `run_trace_digest_workflow` with the exact `workflow_input`.
4. Use the tool result as the workflow outcome.

Input modes:
- `trace`: requires `target_agent_id`, `session_id`, and `trace_id`; returns one digest and does not write the JSONL artifact.
- `batch`: requires `target_agent_id`; processes all of the target's traces, appends digest records, and returns counts. Optional `since_ts` (a unix timestamp) limits to traces after it; `limit` caps how many are scanned. Each run is a clean slate — no checkpoint is read or written.

All latency stats are computed deterministically from runtime_event_log data. No model inference is used for metrics computation.

Rules:
- Do not produce online eval dataset rows from this workflow.
- If the tool returns an error, return an object with `schema_version`, status `"failed"`, and `error`.

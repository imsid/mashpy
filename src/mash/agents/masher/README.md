# Masher

`src/mash/agents/masher` contains Mash's built-in workflow-only Masher worker.

Masher is not a user-invokable subagent. `HostBuilder` registers Masher as a
workflow-only runtime and registers Masher's workflows into the pool. This is on
by default; call `HostBuilder.enable_masher(False)` to opt out. Masher is hidden
from public agent listings and from primary-agent `InvokeSubagent` delegation,
but workflow tasks can still call it through the host runtime client.

Because Masher is registered by default, it is built at pool startup and needs an
LLM provider. `build_llm()` resolves the first configured of `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, then an OSS endpoint via
`OSS_BASE_URL` (which also requires `MASHER_OSS_MODEL` to name the served
tool-calling model, optionally `OSS_API_KEY`). With none of these set, startup
raises. Per-provider model overrides: `MASHER_GEMINI_MODEL`,
`MASHER_OPENAI_MODEL`, `MASHER_ANTHROPIC_MODEL`, `MASHER_OSS_MODEL`.

## Workflows

Masher's built-in workflows are:

```python
WorkflowSpec(workflow_id="masher-trace-digest", tasks=[TaskSpec(task_id="digest-traces", agent_spec=masher_spec)])
WorkflowSpec(workflow_id="masher-online-eval-curation", tasks=[TaskSpec(task_id="curate-online-evals", agent_spec=masher_spec)])
```

The task request contains the normal workflow message fields:

- `workflow_id`
- `workflow_run_id`
- `task_id`
- `workflow_input`
- `task_state`

Masher should call the workflow-specific tool with the request's `workflow_input`
and `task_state`, then return JSON text only:

- `masher-trace-digest`: call `run_trace_digest_workflow`
- `masher-online-eval-curation`: call `run_online_eval_curation_workflow`

## Modes

### Trace Mode

Trace mode operates on one explicit trace.

```json
{
  "mode": "trace",
  "target_agent_id": "primary",
  "session_id": "session-id",
  "trace_id": "trace-id"
}
```

### Incremental Mode

Incremental mode uses `task_state.checkpoints[target_agent_id].last_run_ts` to
find traces whose latest event timestamp is newer than the checkpoint.

```json
{
  "mode": "incremental",
  "target_agent_id": "primary"
}
```

Returned checkpoint state:

```json
{
  "schema_version": 1,
  "checkpoints": {
    "primary": {
      "last_run_ts": 1778871600.0,
      "last_trace_ids": ["trace-id"]
    }
  },
  "processed_trace_count": 1,
  "artifact_path": "/path/to/.mash/masher/trace-digests.jsonl",
  "appended_trace_count": 1
}
```

## Purpose and Artifacts

`masher-trace-digest` is diagnostic: it writes compact summaries with status,
metrics, and notable events. Trace mode returns a digest and does not write the
artifact. Incremental mode writes digest rows to:

```text
<MASH_DATA_DIR>/masher/trace-digests.jsonl
```

`masher-online-eval-curation` is dataset curation only: it writes normalized
online eval examples and does not include digest narrative fields such as
`summary`, `metrics`, or `notable_events`. Trace and incremental mode write rows
to:

```text
<MASH_DATA_DIR>/masher/online-evals.jsonl
```

Both workflows share trace loading and build a span tree
(`build_span_tree`) and trace analysis (`analyze_trace`) from the raw runtime
events. The analysis is deterministic — no LLM inference is used for metrics.
Subagent traces are stitched recursively up to 3 levels deep.

Digest rows (schema v2) include:

- `schema_version`
- `target_agent_id`, `session_id`, `trace_id`
- `status`
- `summary` (deterministic formatted string)
- `timing`: `total_duration_ms`, `cold_start_ms`, `context_load_ms`, `total_think_ms`, `total_tool_ms`, `total_subagent_ms`, `idle_ms`, plus percentage breakdowns
- `tokens`: `input_tokens`, `output_tokens`, `total_tokens`
- `counts`: `step_count`, `tool_call_count`, `tool_error_count`, `event_count`
- `tool_stats`: per-tool count, total/avg/max/min latency, error count
- `step_breakdown`: per-step think/tool/subagent/overhead timing and tool call list
- `slowest_operations`: top 10 slowest spans by duration
- `subagent_traces`: nested child trace analysis (recursive)
- `notable_events`

Online eval rows (schema v2) include:

- `schema_version`
- `target_agent_id`, `session_id`, `trace_id`
- `user_message`, `assistant_response`
- `tools_called`, `tool_call_count`, `step_count`
- `input_tokens`, `output_tokens`
- `timing`: same latency breakdown as digest rows

The artifact is owned by Masher. The workflow framework stores checkpoint state
and DBOS run output, but it does not own Masher artifact files.

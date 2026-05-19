# Masher

`src/mash/agents/masher` contains Mash's built-in workflow-only Masher worker.

Masher is not a user-invokable subagent. `HostBuilder.enable_masher()` registers
Masher as a workflow-only runtime and registers Masher workflows. Masher is
hidden from public agent listings and from primary-agent `InvokeSubagent`
delegation, but workflow tasks can still call it through the host runtime client.

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

Both workflows share trace loading and metric extraction helpers for:

- user message
- assistant response
- tool calls
- step count
- token totals

Digest rows include:

- `schema_version`
- `target_agent_id`
- `session_id`
- `trace_id`
- `status`
- `summary`
- `metrics`
- `notable_events`

Online eval rows include:

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

The artifact is owned by Masher. The workflow framework stores checkpoint state
and DBOS run output, but it does not own Masher artifact files.

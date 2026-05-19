# AGENTS Guide for `src/mash/agents/masher`

## Scope

Masher is Mash's built-in workflow-only trace processing worker.

It is registered by `HostBuilder.enable_masher()` as a workflow agent for
Masher-owned workflows. It must not be exposed as a normal subagent or as a
delegation target for `InvokeSubagent`.

## What Must Stay True

- Masher remains workflow-only.
- `HostBuilder.enable_masher()` registers Masher as a workflow agent and registers
  all built-in Masher workflows.
- Built-in Masher workflows use `TaskSpec(agent_spec=masher_spec)`.
- `workflow_input` is trigger input and is immutable for the run.
- `task_state` is checkpoint state only.
- `masher-trace-digest` stays diagnostic and produces summaries, metrics, and
  notable events.
- `masher-online-eval-curation` stays dataset-focused and produces compact eval
  rows only.
- Shared trace extraction should stay shared across Masher workflows.
- The workflow framework does not own Masher artifacts.
- Masher output must be JSON text only.

## Digest Contract

The shared trace digest object should stay compact and machine-readable:

- `schema_version`
- `target_agent_id`
- `session_id`
- `trace_id`
- `status`
- `summary`
- `metrics`
- `notable_events`

If this schema changes, update:

- `README.md`
- Masher tests
- Any API/CLI workflow status expectations that depend on completed workflow
  output shape

## Online Eval Contract

The online eval object should stay compact and machine-readable:

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

## Change Rules

- Prefer deterministic tools for trace lookup, digest construction, checkpoint
  updates, and JSONL writes.
- Do not add free-form chat or subagent usage behavior back into Masher.
- Do not store user-facing digest or eval records in workflow task state.
- Keep JSONL writes constrained to Masher-owned artifact paths.
- Keep target trace selection based on `target_agent_id`, `session_id`, and
  `trace_id` for trace mode.
- Keep incremental selection based on the per-target checkpoint timestamp.

## Minimal Validation

- `uv run --extra dev pytest -q tests/mash/agents/test_masher.py`
- `uv run --extra dev pytest -q tests/mash/runtime/test_host_integration.py`
- `uv run ruff check src/mash/agents/masher tests/mash/agents/test_masher.py`

# Masher

`src/mash/agents/masher` contains Mash's built-in observability and eval
workflows, plus the workflow-only Masher worker agent that two of them use.

The package is organized along the judgment/computation line the v2 workflow
design draws:

- `pipelines.py` — the workflow definitions: pydantic step models, code-step
  bodies, and the `WorkflowSpec` builders.
- `traces.py` — deterministic trace loading, span analysis, and JSONL artifact
  helpers used by the code steps. No model inference anywhere.
- `context.py` — `MasherRuntimeContext`, the dependency holder code steps close
  over (runtime store, agent pool, eval service, artifact paths).
- `spec.py` — the Masher agent (generation and judging only; it has no tools)
  and `build_masher_workflow_specs`.
- `judge.py` / `score_runner.py` — the score-evals judging contract and its
  durable fan-out `WorkflowStrategy`.

Masher (the agent) is not a user-invokable subagent. It is hidden from public
agent listings and from `InvokeSubagent` delegation; only workflow steps reach
it. `HostBuilder` registers everything by default; opt out with
`enable_masher(False)`.

## Workflows

### `masher-trace-digest` — all code, no LLM

Deterministic latency analysis over an agent's runtime traces.

```text
list-traces (code) -> digest-traces (code) -> append-digests (code)
```

`workflow_input` (`TraceScanInput`): `mode` (`trace` | `batch`, default
`batch`), `target_agent_id`, `session_id`/`trace_id` (required in trace mode),
`since_ts` (batch watermark, default 0), `limit` (default 100).

Trace mode returns the digest in the run result and does not write the
artifact. Batch mode appends digest rows (deduped on
target/session/trace) to `<MASH_DATA_DIR>/masher/trace-digests.jsonl` and
returns counts. The result carries `latest_event_at` — the caller persists it
and passes it back as the next run's `since_ts`; cross-run state lives at the
trigger boundary, never inside the workflow.

### `masher-online-eval-curation` — all code, no LLM

Mechanical extraction of normalized online eval rows from runtime traces.

```text
list-traces (code) -> extract-rows (code) -> append-rows (code)
```

Same `TraceScanInput` contract and watermark behavior. Rows append to
`<MASH_DATA_DIR>/masher/online-evals.jsonl`; trace mode also returns the row as
`record`. Eval rows are compact (`user_message`, `assistant_response`,
`tools_called`, token/step counts, timing) and never include digest narrative
fields.

### `gen-synthetic-evals` — code, agent, code

```text
profile-host (code) -> generate (agent) -> persist-eval (code)
```

`workflow_input` (`GenSyntheticEvalsInput`): `host_id`, `user_guidance`
(optional), `row_count` (default 20, max 100).

`profile-host` reads the host composition and each member agent's declared
`AgentMetadata` from the pool. `generate` is one Masher agent-loop run with the
`gen-synthetic-evals` skill; its structured output is validated against
`GeneratedEval` (row shapes, sampling categories, rubric weights summing
to 1.0). `persist-eval` enforces the exact `row_count` and writes the eval
through the eval service, returning `{eval_id, host_id, dataset_id, rubric_id,
row_count}`. Because the generation output is memoized, a row-count failure is
terminal for the run — start a fresh run to regenerate.

### `score-evals` — strategy (durable fan-out)

Unchanged: a custom `WorkflowStrategy` that loads the eval, snapshots the host,
fans out one durable child workflow per dataset row (host run, then Masher
judge), and persists the experiment. See `score_runner.py`.

## Registration and keyless deployments

`HostBuilder.build()` constructs the Masher spec, binds the pool into its
`MasherRuntimeContext`, and registers the workflows. The two all-code pipelines
never touch an LLM, so they register (and their steps run) even when no
provider is configured. Only the Masher agent and its two agent-dependent
workflows (`gen-synthetic-evals`, `score-evals`) require a provider; on a
keyless deployment they are skipped.

`build_llm()` resolves the first configured of `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, then an OSS endpoint
via `OSS_BASE_URL` (which also requires `MASHER_OSS_MODEL`, optionally
`OSS_API_KEY`). Per-provider model overrides: `MASHER_GEMINI_MODEL`,
`MASHER_OPENAI_MODEL`, `MASHER_ANTHROPIC_MODEL`, `MASHER_OSS_MODEL`.

Registered Masher workflows are attached to every host the builder defines,
appended after any workflows the host attached explicitly, so host
compositions (the Hosts admin tab, `GET /workflow?host=...`) show them. Hosts
defined dynamically after `build()` are not touched.

## Digest contents

Digest rows (schema v2) include `status`, a deterministic `summary` string,
`timing` (total/think/tool/cold-start/context-load/subagent/idle plus
percentages), `tokens`, `counts`, per-tool `tool_stats`, per-step
`step_breakdown`, the top-10 `slowest_operations`, recursively stitched
`subagent_traces` (up to 3 levels), and `notable_events`. All computed
deterministically from `runtime_event_log` data by `traces.py`.

The JSONL artifacts are owned by Masher's code steps. The workflow framework
persists step I/O snapshots and run results in its own tables, but it does not
own the artifact files.

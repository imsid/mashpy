# Masher

`src/mash/agents/masher` contains Mash's built-in observability and eval
workflows, plus dedicated eval generation and judging agents.

The package is organized along the judgment/computation line the workflow
design draws:

- `workflows.py` — all four workflow definitions, typed code-step bodies, and
  `WorkflowSpec` builders.
- `traces.py` — deterministic trace loading, span analysis, and JSONL artifact
  helpers used by the code steps. No model inference anywhere.
- `context.py` — `MasherRuntimeContext`, the dependency holder code steps close
  over (runtime store, agent pool, eval service, artifact paths).
- `spec.py` — `EvalAgentSpec`, `EvalJudgeAgentSpec`, provider selection,
  metadata, and generation skill registration.
- `judge.py` — the run-experiment judging prompt and response contract.

`HostBuilder` registers `eval-agent` and `eval-judge-agent` through the normal
agent path. Every pool also receives all four workflows.

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
`AgentMetadata` from the pool. `generate` is one eval-agent loop run with the
`gen-synthetic-evals` skill; its structured output is validated against
`GeneratedEval` (row shapes, sampling categories, rubric weights summing
to 1.0). `persist-eval` enforces the exact `row_count` and writes the eval
through the eval service, returning `{eval_id, host_id, dataset_id, rubric_id,
row_count}`. Because the generation output is memoized, a row-count failure is
terminal for the run — start a fresh run to regenerate.

### `run-experiment` — code, code, code

```text
prepare-experiment (code) -> execute-rows (code) -> judge-rows (code)
```

The input is `{eval_id, host_id}`. Preparation atomically snapshots the host,
agent specs, rubric, and dataset into an experiment row ledger. Execution and
judging use ordinary async Python fan-out over unfinished rows, running the
first unfinished row alone to warm the provider cache. Row statuses and
deterministic request IDs make both phases replay-safe. Judging uses the
dedicated `eval-judge-agent`; weighted-score arithmetic stays in Python.

## Registration and provider configuration

`HostBuilder.build()` constructs both eval specs, binds the shared runtime
context, registers both as visible agents, and registers all four workflows.

`build_llm()` resolves the first configured of `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, then an OSS endpoint
via `OSS_BASE_URL` (which also requires `EVAL_AGENT_OSS_MODEL`, optionally
`OSS_API_KEY`). Per-provider model overrides: `EVAL_AGENT_GEMINI_MODEL`,
`EVAL_AGENT_OPENAI_MODEL`, `EVAL_AGENT_ANTHROPIC_MODEL`, `EVAL_AGENT_OSS_MODEL`.
The judge accepts corresponding `EVAL_JUDGE_AGENT_*_MODEL` overrides and falls
back to the generation-agent values.

Registered Masher workflows are attached to every host the builder defines,
appended after any workflows the host attached explicitly, so host
compositions (the Hosts admin tab, `GET /workflow?host=...`) show them. The pool
also applies these defaults to hosts defined dynamically after `build()`.

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

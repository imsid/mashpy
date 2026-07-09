# AGENTS Guide for `src/mash/agents/masher`

## Scope

Masher is Mash's built-in observability and eval workflow package: two all-code
trace pipelines, a code→agent→code eval-generation pipeline, a strategy-driven
scoring workflow, and the workflow-only Masher worker agent.

`HostBuilder` registers the workflows by default (opt out with
`enable_masher(False)`). The Masher agent must not be exposed as a normal
subagent or as a delegation target for `InvokeSubagent`.

## What Must Stay True

- Masher (the agent) remains workflow-only, and it only generates and judges —
  it has no tools. Deterministic work belongs in workflow code steps, not in
  agent tools.
- `masher-trace-digest` and `masher-online-eval-curation` are all-code
  pipelines: no LLM anywhere, and they register and run on keyless
  deployments. Do not add an agent step to them without a design decision.
- `gen-synthetic-evals` and `score-evals` require the Masher agent and are
  skipped when no LLM provider is configured.
- Every step is typed: pydantic models on both edges of code steps; agent-step
  output models are the structured-output schema.
- `workflow_input` is trigger input and is immutable for the run. There is no
  cross-run state inside a workflow: batch scans take `since_ts` in and report
  `latest_event_at` out, and the caller owns the watermark.
- `masher-trace-digest` stays diagnostic (summaries, metrics, notable events);
  `masher-online-eval-curation` stays dataset-focused (compact rows only, no
  digest narrative fields).
- Shared trace extraction stays shared (`traces.py`) across both pipelines.
- JSONL artifact writes stay constrained to Masher-owned artifact paths and
  deduped on `(target_agent_id, session_id, trace_id)` so at-least-once step
  replay converges.
- The workflow framework does not own Masher artifacts.

## Change Rules

- Keep step bodies deterministic; anything nondeterministic must be an agent
  step with a validated output model.
- Do not add free-form chat or subagent usage behavior back into Masher.
- Keep trace-mode selection based on `target_agent_id`, `session_id`, and
  `trace_id`; keep batch selection based on the caller-supplied `since_ts`.
- If the digest or eval-row schema changes, update `README.md`, the Masher
  tests, and any API/CLI expectations that depend on completed workflow output
  shape.

## Minimal Validation

- `uv run --extra dev pytest -q tests/mash/agents/test_masher.py`
- `uv run --extra dev pytest -q tests/mash/runtime/test_host_integration.py`
- `uv run ruff check src/mash/agents/masher tests/mash/agents/test_masher.py`

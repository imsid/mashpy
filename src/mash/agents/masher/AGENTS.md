# AGENTS Guide for `src/mash/agents/masher`

## Scope

Masher is Mash's built-in observability and eval workflow package: two all-code
trace pipelines, a code→agent→code eval-generation pipeline, a three-code-step
experiment workflow, and dedicated generation and judging agents.

`HostBuilder` always registers both eval agents as normal visible agents and
registers all four workflows. There is no opt-out or partial-registration mode.

## What Must Stay True

- The eval agent only generates and the eval judge only judges; neither has tools.
- `masher-trace-digest` and `masher-online-eval-curation` are all-code
  pipelines: no LLM anywhere, and they register and run on keyless
  deployments. Do not add an agent step to them without a design decision.
- `gen-synthetic-evals` and `run-experiment` require the generation and judge
  agents respectively. Both are part of every pool.
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

- Code steps may orchestrate deterministic inline agent requests, but their
  task IDs and persistence keys must remain stable across replay.
- Do not add free-form chat or subagent usage behavior to the eval agent.
- Keep trace-mode selection based on `target_agent_id`, `session_id`, and
  `trace_id`; keep batch selection based on the caller-supplied `since_ts`.
- If the digest or eval-row schema changes, update `README.md`, the Masher
  tests, and any API/CLI expectations that depend on completed workflow output
  shape.

## Minimal Validation

- `uv run --extra dev pytest -q tests/mash/agents/test_masher.py`
- `uv run --extra dev pytest -q tests/mash/runtime/test_host_integration.py`
- `uv run ruff check src/mash/agents/masher tests/mash/agents/test_masher.py`

# AGENTS Guide for `src/mash/agents/masher`

## Scope

Masher is Mash's built-in eval workflow package: a code→agent→code
eval-generation pipeline, a three-code-step experiment workflow, and dedicated
generation and judging agents.

Masher is off unless the app calls `HostBuilder.enable_evals()`. Then the
builder registers both eval agents as normal visible agents and both workflows,
attached to every host. There is no partial-registration mode.

## What Must Stay True

- The eval agent only generates and the eval judge only judges; neither has tools.
- `gen-synthetic-evals` and `run-experiment` require the generation and judge
  agents respectively. `enable_evals()` registers all four together.
- Every step is typed: pydantic models on both edges of code steps; agent-step
  output models are the structured-output schema.
- `workflow_input` is trigger input and is immutable for the run. There is no
  cross-run state inside a workflow.

## Change Rules

- Code steps may orchestrate deterministic inline agent requests, but their
  task IDs and persistence keys must remain stable across replay.
- Do not add free-form chat or subagent usage behavior to the eval agent.
- If the generated eval or experiment result shape changes, update `README.md`,
  the Masher tests, and any API/CLI expectations that depend on completed
  workflow output shape.

## Minimal Validation

- `uv run --extra dev pytest -q tests/mash/agents/test_masher.py tests/mash/agents/test_score_evals.py`
- `uv run --extra dev pytest -q tests/mash/runtime/test_host_integration.py`
- `uv run ruff check src/mash/agents/masher tests/mash/agents/test_masher.py`

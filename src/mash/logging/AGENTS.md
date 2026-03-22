# AGENTS Guide for `src/mash/logging`

## What Must Stay True
- Event logs remain machine-parseable JSONL.
- Trace IDs continue to flow through the trace context helpers.
- Logging stays usable from core, runtime, API, and tool layers.

## Change Rules
- Keep event definitions centralized here rather than spreading ad hoc log payloads across the codebase.
- Preserve field stability where tests, tooling, or built-in agents rely on those event shapes.
- If trace or event payload formats change, update the affected integrations together.

## Minimal Validation
- `python -m compileall src/mash/logging`
- Verify one event emission path and one trace propagation path.

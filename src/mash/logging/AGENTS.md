# AGENTS Guide for `src/mash/logging`

## Scope
Structured event schema and JSONL event logger.

## Invariants
- Event dataclasses are immutable (`frozen=True`) and serialize via `to_dict()`.
- Logger writes exactly one JSON object per line.
- Event fields should stay stable for downstream telemetry tooling (`event_type`, `event_class`, `app_id`, `session_id`, `ts`, `payload`).

## Adding Events
- Prefer extending existing event classes where possible.
- Keep added fields optional unless universally available.
- Avoid non-serializable payload values.

## Trace Context
- Trace ID propagation uses thread-local helpers in `trace_context.py`.
- Any new async/threaded logging path should explicitly manage trace context.

# AGENTS Guide for `src/mash/telemetry`

## Scope
Telemetry backend (`http.server` + SSE) and React frontend for viewing JSONL traces.

## Backend Invariants
- `/api/logs` returns JSON snapshot with `events` and resolved `path`.
- `/api/stream` emits `data: <json>\n\n` events for valid JSONL lines.
- CORS remains open (`Access-Control-Allow-Origin: *`) for local development.

## Frontend Expectations
- UI groups by `session_id`, then by `trace_id`, and uses `event_class` for styling.
- Event payloads are treated as append-only live stream data.
- Keep timeline filters resilient to partial/malformed events.

## Operational Notes
- Default run mode expects an explicit log path (`--log`) or query parameter.
- Preserve lightweight dependencies; this is intended for local observability.

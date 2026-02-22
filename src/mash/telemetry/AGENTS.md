# AGENTS Guide for `src/mash/telemetry`

## Scope
Telemetry backend (`http.server` + SSE) and React frontend for viewing JSONL traces, plus memory-store search from the telemetry UI.

## Backend Invariants
- `/api/logs` returns JSON snapshot with `events` and resolved `path`.
- `/api/stream` emits `data: <json>\n\n` events for valid JSONL lines.
- `/api/search` is the telemetry memory-search endpoint and must call `MemorySearchService.search()`.
- `/api/search` requires `q` and `app_id`; `session_id` remains optional for session-scoped searches.
- `/api/search` returns `503` when memory search is not configured (no `--memory-db`).
- CORS remains open (`Access-Control-Allow-Origin: *`) for local development.

## Frontend Expectations
- UI groups by `session_id`, then by `trace_id`, and uses `event_class` for styling.
- Event payloads are treated as append-only live stream data.
- Keep timeline filters resilient to partial/malformed events.
- Memory search UI derives `app_id` from the selected session's telemetry events.
- Memory search is manual-submit (no live/debounced queries) and supports session/app scope.
- Search clicks should deep-link into the existing trace timeline using `turn_id == trace_id` when available.
- If a memory hit trace is missing from the loaded log file, show a non-blocking UI message instead of failing.

## Operational Notes
- Default run mode expects an explicit log path (`--log`) or query parameter.
- Memory search requires a SQLite memory DB path via `--memory-db`; telemetry remains usable without it.
- Telemetry currently configures memory search as keyword-only for `SQLiteStore` (semantic search is not yet implemented).
- Preserve lightweight dependencies; this is intended for local observability.

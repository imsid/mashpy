# API

Lightweight reference for the HTTP surface exposed by `src/mash/api/app.py`.

This README is intended to be prompt-cache friendly for the `api-copilot` agent: it lists the exposed routes, the common request/response conventions, and the main query/body fields without duplicating full OpenAPI schemas.

## Base Surface
- Default bind: `http://127.0.0.1:8000`
- API prefix: `/api/v1`
- OpenAPI JSON: `/openapi.json`
- Swagger UI: `/docs`
- ReDoc: `/redoc`
- Root discovery: `/`
- Telemetry SPA: `/telemetry`, `/telemetry/`, `/telemetry/{path:path}`
- Telemetry static assets: `/telemetry/assets/...`

## Auth
- If `MashHostConfig.api_key` is unset, API routes are open.
- If `api_key` is set, every `/api/v1/*` route requires one of:
  - `Authorization: Bearer <token>`
  - `X-API-Key: <token>`
  - `mash_api_key` cookie
- `/telemetry` sets the `mash_api_key` cookie when an API key is configured so the SPA can call protected API routes.

## Response Shape
- Success responses use the envelope: `{"data": ...}`
- Error responses use the envelope:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "human readable message",
    "details": {}
  }
}
```

- Validation errors return `422` with `code = "VALIDATION_ERROR"`.
- Runtime client failures return `502` with `code = "RUNTIME_CLIENT_ERROR"`.

## Bodies
- `InvokeRequest`

```json
{
  "message": "required non-empty string",
  "session_id": "optional string",
  "turn_metadata": {},
  "timeout_ms": 30000
}
```

- `SubmitRequest`

```json
{
  "message": "required non-empty string",
  "session_id": "optional string",
  "turn_metadata": {}
}
```

- `PreferencesUpdateRequest`

```json
{
  "preferences": {}
}
```

- `AppDataSetRequest`

```json
{
  "value": "any JSON value"
}
```

- `CompactSessionRequest`

```json
{
  "reason": "manual",
  "session_total_tokens_reset": 0
}
```

## Endpoints

### Discovery And Meta

`GET /`
- Returns service-level discovery info:
  - `service`
  - `api.version`
  - `api.base`
  - `api.openapi`
  - `api.docs`

`GET /api/v1/health`
- Health and deployment summary.
- Returns:
  - `status`, `service`, `api_version`
  - `deployment.primary_agent_id`
  - `deployment.agents`
  - `primary_agent`
  - `observability.enabled`
  - `observability.memory.configured`
  - `observability.memory.search_available`
  - `observability.memory.path`
  - `observability.memory.default_limit`

`GET /api/v1/agents`
- Lists hosted agents.
- Returns `agents` and `primary_agent_id`.

`GET /api/v1/agents/{agent_id}`
- Returns agent metadata plus current session info.
- Path params:
  - `agent_id`
- Returns:
  - `agent`
  - `session`

### Runtime Invocation

`POST /api/v1/agents/{agent_id}/invoke`
- Synchronous request/response invocation.
- Path params:
  - `agent_id`
- Body: `InvokeRequest`
- Returns the runtime invoke result in `data`.
- Returns `504 REQUEST_TIMEOUT` if the invoke call times out.

`POST /api/v1/agents/{agent_id}/requests`
- Submits an async request and returns a request id.
- Path params:
  - `agent_id`
- Body: `SubmitRequest`
- Returns `request_id`.

`GET /api/v1/agents/{agent_id}/requests/{request_id}/events`
- Server-Sent Events stream for async request progress.
- Path params:
  - `agent_id`
  - `request_id`
- Response type: `text/event-stream`
- Emits runtime events with:
  - `event: <event_name>`
  - `data: <json payload>`
- Terminates when event name is `request.completed` or `request.error`.

### Sessions

`GET /api/v1/agents/{agent_id}/sessions`
- Lists runtime sessions for one agent.
- Path params:
  - `agent_id`
- Returns `sessions`.

`GET /api/v1/agents/{agent_id}/sessions/{session_id}`
- Returns session info for a specific session id.
- Path params:
  - `agent_id`
  - `session_id`

`GET /api/v1/agents/{agent_id}/sessions/{session_id}/history`
- Returns session history turns.
- Path params:
  - `agent_id`
  - `session_id`
- Query params:
  - `limit` optional
- Returns `turns`.

`POST /api/v1/agents/{agent_id}/sessions/{session_id}/compact`
- Compacts a session and resets token accounting if requested.
- Path params:
  - `agent_id`
  - `session_id`
- Body: `CompactSessionRequest`
- Returns:
  - `summary_text`
  - `turn_id`

### Session Preferences

`GET /api/v1/agents/{agent_id}/sessions/{session_id}/preferences`
- Reads session preferences.
- Path params:
  - `agent_id`
  - `session_id`
- Returns `preferences`.

`PUT /api/v1/agents/{agent_id}/sessions/{session_id}/preferences`
- Replaces session preferences.
- Path params:
  - `agent_id`
  - `session_id`
- Body: `PreferencesUpdateRequest`
- Returns `ok: true`.

### Session App Data

`GET /api/v1/agents/{agent_id}/sessions/{session_id}/app-data`
- Lists app-data items for a session.
- Path params:
  - `agent_id`
  - `session_id`
- Returns `items`.

`GET /api/v1/agents/{agent_id}/sessions/{session_id}/app-data/{key}`
- Reads one app-data value.
- Path params:
  - `agent_id`
  - `session_id`
  - `key`
- Returns `value`.

`PUT /api/v1/agents/{agent_id}/sessions/{session_id}/app-data/{key}`
- Sets one app-data value.
- Path params:
  - `agent_id`
  - `session_id`
  - `key`
- Body: `AppDataSetRequest`
- Returns `ok: true`.

`DELETE /api/v1/agents/{agent_id}/sessions/{session_id}/app-data/{key}`
- Deletes one app-data value.
- Path params:
  - `agent_id`
  - `session_id`
  - `key`
- Returns `deleted: <bool>`.

### Observability

These endpoints require `enable_observability = True`. Memory search also requires `observability_memory_db_path` to be configured and searchable.

`GET /api/v1/telemetry/events`
- Reads recent JSONL telemetry events for one agent.
- Query params:
  - `agent_id` required
  - `limit` optional, clamped to `1..20000`
- Returns:
  - `events`
  - `path`
  - `agent_id`
  - `limit`

`GET /api/v1/telemetry/events/stream`
- Tails telemetry events as SSE.
- Query params:
  - `agent_id` required
- Response type: `text/event-stream`
- Emits:
  - `data: <raw json line>`
- Emits `: keep-alive` frames while idle.

`GET /api/v1/telemetry/memory/search`
- Searches observability memory records.
- Query params:
  - `q` required non-empty string
  - `app_id` required non-empty string
  - `session_id` optional
  - `limit` optional, clamped to `1..50`
- Returns:
  - `results`
  - `app_id`
  - `session_id`
  - `query`
  - `limit`

## Error Cases Worth Remembering
- `401 UNAUTHORIZED`: missing or wrong API key
- `404 AGENT_NOT_FOUND`: unknown `agent_id`
- `400 INVALID_REQUEST`: blank required path/body values after trimming
- `404 LOG_FILE_NOT_FOUND`: telemetry log path missing
- `503 OBSERVABILITY_DISABLED`: telemetry APIs disabled
- `503 MEMORY_SEARCH_UNAVAILABLE`: memory DB/search service not configured
- `400 SEARCH_VALIDATION_ERROR`: invalid memory search arguments
- `503 SEARCH_UNAVAILABLE`: search backend not available
- `500 SEARCH_FAILED`: unexpected memory search failure

## Source Of Truth
- The route definitions live in [app.py](/Users/sid/Projects/mashpy/src/mash/api/app.py).
- The telemetry SPA routes live in [telemetry_ui.py](/Users/sid/Projects/mashpy/src/mash/api/telemetry_ui.py).
- The default host config lives in [config.py](/Users/sid/Projects/mashpy/src/mash/api/config.py).

## Verification Notes
- Route inventory cross-checked against `create_app(...).openapi()` plus the non-schema routes mounted on the FastAPI app.
- `http://127.0.0.1:8000/openapi.json` was not available during this update, so the live endpoint could not be fetched; the README was derived from the local app registration instead.

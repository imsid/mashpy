# API

Lightweight reference for the HTTP surface composed by `src/mash/api/app.py`
and implemented in `src/mash/api/routes/`.

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
- `SubmitRequest`

```json
{
  "message": "required non-empty string",
  "session_id": "required non-empty string",
  "structured_output": "optional JSON-schema object"
}
```

- `CompactSessionRequest`

```json
{
  "reason": "manual",
  "session_total_tokens_reset": 0
}
```

- `RegisterAgentSkillRequest`

```json
{
  "type": "required non-empty string",
  "name": "required non-empty string",
  "description": "optional string",
  "location": "optional string (filesystem path)",
  "content": "optional string (inline markdown)"
}
```

At least one of `location` or `content` must be set (enforced by `Skill`
validation).

- `RegisterAgentWorkflowRequest`

```json
{
  "workflow_id": "required non-empty string",
  "tasks": [
    {
      "task_id": "required non-empty string",
      "agent_id": "required non-empty string",
      "structured_output": "optional JSON-schema object"
    }
  ],
  "metadata": "optional JSON object",
  "task_message": {
    "skill_name": "required non-empty string"
  }
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
  - `observability.memory.search_available`
  - `observability.memory.default_limit`

`GET /api/v1/agent`
- Lists hosted agents.
- Returns `agents` and `primary_agent_id`.

`GET /api/v1/agent/{agent_id}`
- Returns agent metadata plus current session info.
- Path params:
  - `agent_id`
- Returns:
  - `agent`
  - `session`

### Runtime Requests

`POST /api/v1/agent/{agent_id}/request`
- Submits an async request and returns a request id.
- Path params:
  - `agent_id`
- Body: `SubmitRequest`
- Returns `request_id`.
- Optionally accepts a `structured_output` JSON-schema object on the body; when
  set, the response stream's `request.completed` payload includes a
  `structured_output` field alongside `text`.

`GET /api/v1/agent/{agent_id}/request/{request_id}/events`
- Server-Sent Events stream for async request progress.
- Path params:
  - `agent_id`
  - `request_id`
- Response type: `text/event-stream`
- Emits runtime events with:
  - `event: <event_name>`
  - `data: <json payload>`
- Terminates when event name is `request.completed` or `request.error`.

`GET /api/v1/agent/{agent_id}/request/{request_id}/status`
- Returns the current DBOS workflow status for a request.
- Path params:
  - `agent_id`
  - `request_id`
- Returns:
  - `request_id`
  - `workflow_id`
  - `status`: `pending` | `completed` | `failed` | `cancelled` | `queued`
  - `dbos_status`: raw DBOS status string
  - `error`: optional error message
  - `recovery_attempts`: optional number of DBOS recovery attempts
- Use this to check request state when the SSE stream goes silent (e.g., after a
  process crash).

`POST /api/v1/agent/{agent_id}/request/{request_id}/resume`
- Resumes a failed or cancelled request by setting the DBOS workflow back to
  PENDING for recovery.
- Path params:
  - `agent_id`
  - `request_id`
- Returns:
  - `request_id`
  - `workflow_id`
  - `status`: `resumed` | `completed` | `pending` | current status
  - `previous_status`: the status before resume (only when `status` is `resumed`)
  - `message`: human-readable description
- If the request is already completed or pending, returns informational status
  without changing state.

### Dynamic Publishing

`POST /api/v1/agent/{agent_id}/skill`
- Registers a dynamic or filesystem-backed skill on the named agent.
- Path params:
  - `agent_id`
- Body: `RegisterAgentSkillRequest`
- Returns:
  - `agent_id`
  - `skill_name`
- Idempotent: if the skill name is already registered for that agent, the
  request is a no-op (no error).

`POST /api/v1/agent/{agent_id}/workflow`
- Publishes (upserts) a workflow definition owned by the named agent.
- Path params:
  - `agent_id`
- Body: `RegisterAgentWorkflowRequest`
- Returns:
  - `agent_id`
  - `workflow_id`
- Upsert semantics: re-registering the same `workflow_id` replaces the live
  definition. Unregistration is only available through the in-process host
  API (`AgentHost.unregister_agent_workflow`), not HTTP.

### Sessions

`GET /api/v1/agent/{agent_id}/sessions`
- Lists runtime sessions for one agent.
- Path params:
  - `agent_id`
- Returns `sessions`.

`GET /api/v1/agent/{agent_id}/sessions/{session_id}`
- Returns session info for a specific session id.
- Path params:
  - `agent_id`
  - `session_id`

`GET /api/v1/agent/{agent_id}/sessions/{session_id}/history`
- Returns session history turns.
- Path params:
  - `agent_id`
  - `session_id`
- Query params:
  - `limit` optional
- Returns `turns`.

`GET /api/v1/agent/{agent_id}/sessions/{session_id}/signals`
- Returns the session's signal surface for one agent.
- Path params:
  - `agent_id`
  - `session_id`
- Query params:
  - `limit` optional
- Returns:
  - `agent_id`
  - `session_id`
  - `definitions`: map keyed by signal name describing the runtime's built-in signals
  - `turns`: chronological per-turn signal payloads with `turn_id`, `created_at`, and `signals`
- Notes:
  - `definitions` is always returned, even when the session has no turns.
  - `turns[*].signals` contains the persisted per-turn values and may be `{}`.

`POST /api/v1/agent/{agent_id}/sessions/{session_id}/compact`
- Compacts a session and resets token accounting if requested.
- Path params:
  - `agent_id`
  - `session_id`
- Body: `CompactSessionRequest`
- Returns:
  - `summary_text`
  - `turn_id`

`GET /api/v1/agent/{agent_id}/session/{session_id}/trace/{trace_id}/reasoning`
- Returns the compact CLI-style reasoning trace for one trace.
- Requires observability to be enabled because it reads runtime event logs.
- Path params:
  - `agent_id`
  - `session_id`
  - `trace_id`
- Returns:
  - `status`
  - `steps`
  - `summary`
  - `source`
  - `agent_id`
  - `session_id`
  - `trace_id`

### Workflows

`GET /api/v1/workflow`
- Lists registered host workflows.
- Returns `workflows`.

`POST /api/v1/workflow/{workflow_id}/run`
- Starts a workflow run.
- Path params:
  - `workflow_id`
- Body fields:
  - `dedup_key` optional string
  - `input` optional JSON object
- Returns:
  - `run_id`
  - `workflow_id`
  - `status`

`GET /api/v1/workflow/{workflow_id}/runs`
- Lists previous run summaries for one workflow.
- Path params:
  - `workflow_id`
- Query params:
  - `status` optional public status filter; only `completed` returns memory-backed runs
  - `start_time`, `end_time` optional memory turn time bounds
  - `limit` optional, default `50`, clamped to `1..200`
  - `offset` optional, default `0`
  - `sort_desc` optional, default `true`
- Returns `workflow_id` and `runs`; each run includes `run_id`, `workflow_id`, `dedup_key`, `status`, timestamps, `error`, and `summary`.
- Does not include run `output`; call the run detail endpoint for results.

`GET /api/v1/workflow/{workflow_id}/runs/{run_id}`
- Returns one workflow run status and output.
- Path params:
  - `workflow_id`
  - `run_id`

`GET /api/v1/workflow/{workflow_id}/runs/{run_id}/events`
- Server-Sent Events stream for workflow task runtime events.
- Path params:
  - `workflow_id`
  - `run_id`

### Observability

These endpoints require `enable_observability = True`. Memory search uses the target agent's `memory_store`.

Backend API request logs are persisted separately in `api_event_log` when `api_logging_enabled = True`.

`GET /api/v1/telemetry/events`
- Reads recent canonical runtime events for one agent.
- Query params:
  - `agent_id` required
  - `limit` optional, clamped to `1..20000`
- Returns:
  - `events`
  - `source`
  - `agent_id`
  - `limit`

`GET /api/v1/telemetry/events/stream`
- Tails canonical runtime events as SSE.
- Query params:
  - `agent_id` required
- Response type: `text/event-stream`
- Emits:
  - `data: <serialized runtime event>`
- Emits `: keep-alive` frames while idle.

`GET /api/v1/telemetry/api/events`
- Reads recent backend API request/response events.
- Query params:
  - `method` optional
  - `path` optional exact path
  - `status_code` optional
  - `from_ts`, `to_ts` optional Unix timestamp bounds
  - `after_event_id` optional cursor
  - `limit` optional, clamped to `1..20000`
- Returns:
  - `events`
  - `source = "api_event_log"`
  - `limit`

`POST /api/v1/telemetry/api/events/search`
- Searches backend API request/response events with structured filters.
- Body fields:
  - same fields as `GET /telemetry/api/events`
  - `path_prefix` optional
  - `status_code_min`, `status_code_max` optional

`GET /api/v1/telemetry/api/events/stream`
- Tails backend API request/response events as SSE.
- Query params:
  - same filters as `GET /telemetry/api/events`
- Emits:
  - `data: <serialized API event>`
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
- `503 OBSERVABILITY_DISABLED`: telemetry APIs disabled
- `503 MEMORY_SEARCH_UNAVAILABLE`: memory search unavailable for the target agent
- `400 SEARCH_VALIDATION_ERROR`: invalid memory search arguments
- `503 SEARCH_UNAVAILABLE`: search backend not available
- `500 SEARCH_FAILED`: unexpected memory search failure
- `400 INVALID_STRUCTURED_OUTPUT`: `structured_output` is neither a dict nor a Pydantic-serialized schema
- `400 INVALID_AGENT_SKILL`: skill validation failed (for example, missing both `location` and `content`)
- `400 INVALID_AGENT_WORKFLOW`: workflow validation failed (duplicate task ids, missing `task_message` fields, agent not registered, etc.)
- `404 REQUEST_NOT_FOUND`: unknown `request_id` for status or resume operations

## Source Of Truth
- App composition, auth, lifespan, and exception handlers live in [app.py](/Users/sid/Projects/mashpy/src/mash/api/app.py).
- Shared route helpers live in [routes/common.py](/Users/sid/Projects/mashpy/src/mash/api/routes/common.py).
- Agent/session routes live in [routes/agent.py](/Users/sid/Projects/mashpy/src/mash/api/routes/agent.py).
- Workflow routes live in [routes/workflow.py](/Users/sid/Projects/mashpy/src/mash/api/routes/workflow.py).
- Telemetry routes live in [routes/telemetry.py](/Users/sid/Projects/mashpy/src/mash/api/routes/telemetry.py).
- The telemetry SPA routes live in [telemetry_ui.py](/Users/sid/Projects/mashpy/src/mash/api/telemetry_ui.py).
- The default host config lives in [config.py](/Users/sid/Projects/mashpy/src/mash/api/config.py).

## Verification Notes
- Route inventory cross-checked against `create_app(...).openapi()` plus the non-schema routes mounted on the FastAPI app.
- `http://127.0.0.1:8000/openapi.json` was not available during this update, so the live endpoint could not be fetched; the README was derived from the local app registration instead.

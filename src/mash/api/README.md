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
- Admin dashboard SPA: `/admin`, `/admin/`, `/admin/{path:path}` (mounted only when its bundle is built into `static/admin`)
- Admin static assets: `/admin/assets/...`

## Auth
- If `MashHostConfig.api_key` is unset, API routes are open.
- If `api_key` is set, every `/api/v1/*` route requires one of:
  - `Authorization: Bearer <token>`
  - `X-API-Key: <token>`
  - `mash_api_key` cookie
- `/admin` sets the `mash_api_key` cookie when an API key is configured so the SPA can call protected API routes.

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
  - `deployment.agents`
  - `deployment.hosts`
  - `observability.enabled`
  - `observability.memory.search_available`
  - `observability.memory.default_limit`

`GET /api/v1/agent`
- Lists pooled agents.
- Returns `agents` and `hosts`.

### Hosts

`PUT /api/v1/hosts/{host_id}`
- Defines or replaces a host composition over the pool (idempotent).
- Body: `DefineHostRequest` (`primary`, `subagents`, `workflows`)
- Returns the merged host view (members joined with pool metadata).
- Validation failures return `400 INVALID_HOST`.

`GET /api/v1/hosts`
- Lists defined hosts.

`GET /api/v1/hosts/{host_id}`
- Returns the merged host view; unknown ids return `404 HOST_NOT_FOUND`.

`POST /api/v1/hosts/{host_id}/request`
- Submits a request to the host: it routes to the host's primary agent with
  the host's composition snapshotted onto the request.
- Body: `HostSubmitRequest` (`message`, `session_id`, optional
  `structured_output`, optional `context`)
- `context` is a freeform string the host appends to the primary agent's
  system prompt for this request only (after the subagent routing block).
  Use it to pass per-request/session facts like user profile, workspace
  state, or the current date.
- Returns `request_id`, `agent_id` (the primary), and `session_id`. Stream
  results from `GET /api/v1/agent/{agent_id}/request/{request_id}/events`.

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
- Streamed token output arrives as `event: agent.trace` frames whose
  `data.event_type` is `llm.response.delta`, with the coalesced text chunk at
  `data.payload.payload.text`. Concatenate these in arrival order to render the
  answer live before the terminal event. The final, authoritative response is
  still delivered on `request.completed`.
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
  - `turns`: chronological per-turn signal payloads with `trace_id`, `created_at`, and `signals`
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
  - `trace_id`

`GET /api/v1/agent/{agent_id}/session/{session_id}/trace/{trace_id}/reasoning`
- Returns the compact CLI-style reasoning trace for one trace.
- Requires observability to be enabled because it reads runtime event logs.
- Path params:
  - `agent_id`
  - `session_id`
  - `trace_id`
- Returns:
  - `status`
  - `assistant_blocks` — structured content blocks from the terminal response (same shape as `response_payload.assistant_blocks` on `request.completed`); empty list when the provider does not emit blocks
  - `steps` — each step includes:
    - `assistant_text` — flat text extracted from the LLM response for this step
    - `assistant_blocks` — structured content blocks for this step, including `thinking` blocks from extended-thinking models; empty list when absent
    - `tool_calls`, `token_usage`, `think_duration_ms`, `act_duration_ms`, `total_duration_ms`
  - `summary`
  - `source`
  - `agent_id`
  - `session_id`
  - `trace_id`

### Workflows

`GET /api/v1/workflow`
- Lists registered host workflows.
- Returns catalog summaries with `workflow_id`, display fields, pipeline or
  strategy mode, step counts, a step preview, history availability, and the
  latest stored pipeline run.

`GET /api/v1/workflow/{workflow_id}`
- Returns the complete registered definition: workflow input schema, metadata,
  ordered step schemas and execution details, or the custom strategy name.

`POST /api/v1/workflow/{workflow_id}/run`
- Starts a workflow run.
- Path params:
  - `workflow_id`
- Body fields:
  - `dedup_key` optional string
  - `input` optional JSON object
  - `session_id` optional; the run executes under this session (e.g. the REPL
    session) so it is a trace within it. Absent → a fresh per-run session.
- Returns:
  - `run_id`
  - `workflow_id`
  - `status`

`GET /api/v1/workflow/{workflow_id}/runs`
- Lists previous run summaries for one workflow.
- Path params:
  - `workflow_id`
- Query params:
  - `status` optional public status filter
  - `start_time`, `end_time` optional creation-time bounds
  - `limit` optional, default `50`, clamped to `1..200`
  - `offset` optional, default `0`
  - `sort_desc` optional, default `true`
- Returns `workflow_id`, summary `runs`, and `limit`, `offset`, and `has_more`.
- Does not include the run result; call the run detail endpoint for it.

`GET /api/v1/workflow/{workflow_id}/runs/{run_id}`
- Returns one workflow run with immutable `workflow_input`, `session_id`, final
  `result`, and ordered step snapshots.
- Path params:
  - `workflow_id`
  - `run_id`

`POST /api/v1/workflow/{workflow_id}/runs/{run_id}/resume`
- Resumes a failed step pipeline under the same run id.

`GET /api/v1/workflow/{workflow_id}/runs/{run_id}/step-events`
- Returns the persisted step lifecycle audit, including code steps.

`GET /api/v1/workflow/{workflow_id}/runs/{run_id}/events`
- Server-Sent Events stream for step lifecycle and terminal workflow events.
- Path params:
  - `workflow_id`
  - `run_id`

### Feedback

These endpoints are always available; they do not depend on `enable_observability`. Feedback is stored in the `runtime_feedback` table next to the runtime event log.

`POST /api/v1/feedback`
- Records one piece of user feedback with its session context.
- Body fields:
  - `agent_id` required
  - `message` required
  - `feedback_type` optional, default `text`
  - `host_id`, `session_id`, `request_id`, `trace_id` optional context
  - `context` optional JSON object for anything else worth keeping
- Returns:
  - `feedback`: the stored record, including the assigned `feedback_id` and `created_at`

`GET /api/v1/feedback`
- Lists feedback for one agent, most recent first.
- Query params:
  - `agent_id` required
  - `after` required unix timestamp; only records created after it are returned (pass `0` to read from the beginning)
  - `before` optional unix timestamp upper bound
  - `session_id` optional
  - `feedback_type` optional
  - `q` optional full-text query over the message, ranked the same way as memory keyword search
  - `limit` optional, server default, clamped to `1..1000`
- Returns:
  - `feedback`: list of stored records
  - `agent_id`, `after`, `before`, `limit`

### Evals

Read/manage surface for synthetic evals (see `src/mash/evals/README.md` for the
data model). These endpoints do not depend on `enable_observability`, but they
require a configured `MASH_DATABASE_URL`; without one every route returns `503
EVALS_NOT_AVAILABLE`. Generation and scoring are not HTTP-native: they run as
the `gen-synthetic-evals` and `score-evals` workflows through the normal
`POST /api/v1/workflow/{workflow_id}/run` route.

`GET /api/v1/evals`
- Lists evals, most recent first.
- Query params:
  - `host_id` optional filter
  - `limit` optional, default 50, clamped to `1..200`
  - `offset` optional, default 0
- Returns:
  - `evals`: list of eval records (`eval_id`, `host_id`, `user_guidance`, `dataset_id`, `rubric_id`, `created_at`)
  - `total`: count of returned records

`GET /api/v1/evals/{eval_id}`
- One eval with its full dataset and rubric.
- Returns:
  - `eval`: the eval record
  - `rows`: dataset rows (`row_id`, `input`, `scenario_description`, `sampling_category`, `expected_behavior`)
  - `rubric`: scoring rubric (`global_scoring_prompt`, weighted `criteria`)
  - `locked`: true once the eval has at least one experiment
- `404 EVAL_NOT_FOUND` for unknown ids.

`DELETE /api/v1/evals/{eval_id}`
- Deletes the eval with its dataset, rubric, experiments, and runs.
- Returns `eval_id` and `deleted: true`; `404 EVAL_NOT_FOUND` for unknown ids.

`PUT /api/v1/evals/{eval_id}/rubric`
- Replaces the rubric criteria while the eval is unlocked.
- Body fields:
  - `criteria` required non-empty list of criterion objects (`name`, `description`, `weight`, `scoring_prompt`, optional `scale_min`/`scale_max`)
- Returns the updated `rubric`.
- `409 EVAL_LOCKED` once the eval has experiments — results stay comparable
  because the measure can no longer change; generate a new eval instead.

`GET /api/v1/evals/{eval_id}/experiments`
- Lists experiments for one eval, most recent first.
- Query params:
  - `limit` optional, default 20, clamped to `1..100`
  - `offset` optional, default 0
- Returns:
  - `experiments`: experiment records (`experiment_id`, `status`, `host_composition`, `agent_spec_snapshot`, `created_at`, `completed_at`)
  - `total`: count of returned records

`GET /api/v1/evals/{eval_id}/experiments/{experiment_id}`
- One experiment with aggregate results.
- Returns:
  - `experiment`: the experiment record, including the host composition and per-agent spec snapshot captured at run start
  - `aggregate`: `mean_score`, per-criterion means, run counts, and `operational` cost rollups (latency, steps, tokens including cache read/write, LLM/tool calls, subagent steps)
- `404 EXPERIMENT_NOT_FOUND` for unknown ids.

`GET /api/v1/evals/{eval_id}/experiments/compare`
- Side-by-side comparison of two experiments on the same eval.
- Query params:
  - `baseline` required experiment id
  - `control` required experiment id
- Returns paired per-row runs, score and operational deltas, and the agent-spec
  diff between the two snapshots (`diff_agent_specs`).

`GET /api/v1/evals/{eval_id}/experiments/{experiment_id}/runs`
- Per-row results for one experiment.
- Query params:
  - `limit` optional, default 100, clamped to `1..500`
  - `offset` optional, default 0
- Returns:
  - `runs`: one record per dataset row — `input`, `actual_output`, `weighted_score`, per-criterion `scores` with rationales, `session_id` (links the row's execution to the telemetry surface), `error` when the row could not be scored, and `metrics` (per-row operational cost)
  - `total`: count of returned records

### Observability

These endpoints require `enable_observability = True`. Memory search uses the target agent's `memory_store`.

Backend API request logs are persisted separately in `api_event_log` when `api_logging_enabled = True`.

`GET /api/v1/telemetry/events`
- Reads recent canonical runtime events for one agent.
- Query params:
  - `agent_id` required
  - `session_id`, `trace_id`, `host_id` optional filters — `host_id` selects
    events from requests routed through that host composition
  - `limit` optional, clamped to `1..20000`
- Returns:
  - `events` (each carries `host_id`, null for bare-agent requests)
  - `source`
  - `agent_id`
  - `session_id`, `trace_id`, `host_id`
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

`POST /api/v1/telemetry/command-events`
- Ingests one CLI command lifecycle event from the REPL into the agent's runtime
  event log. The remote shell posts these best-effort as `/commands` run.
- Body fields:
  - `agent_id` required
  - `event_type` required, must start with `command.` (e.g. `command.start`,
    `command.complete`, `command.error`)
  - `session_id`, `host_id`, `trace_id` optional context
  - `command_name`, `args`, `duration_ms`, `error` optional command detail
  - `ts` optional unix timestamp; defaults to ingest time
- Returns:
  - `event`: the stored runtime event (command detail lives under `payload`)
- Errors:
  - `400 INVALID_EVENT_TYPE`: `event_type` does not start with `command.`

`GET /api/v1/telemetry/command-events`
- Lists CLI command events for one agent, most recent first.
- Query params:
  - `agent_id` required
  - `session_id` optional
  - `limit` optional, clamped to `1..2000`
- Returns:
  - `events`: runtime events with `event_type` prefixed `command.`; each carries
    `command_name`, `args`, `duration_ms`, and `error` under `payload`
  - `source`, `agent_id`, `session_id`, `limit`

`GET /api/v1/telemetry/sessions`
- Rolls up sessions from the runtime event log, most recent activity first. A
  session is the container for traces; its owner is the agent of its earliest
  event (the primary for a chat session). Traces within a session may run on
  other agents (subagents, cross-agent workflow tasks).
- Query params:
  - `agent_id` optional; when set, scopes to sessions owned by that agent
  - `limit` optional, clamped to `1..2000`
- Returns:
  - `sessions`: list of `{ session_id, owner_agent_id, host_id, started_at, latest_event_at, trace_count, total_tokens, cache_read_tokens, cache_write_tokens }`
  - `source`, `agent_id`, `limit`

`GET /api/v1/telemetry/traces`
- Lists recent traces, ordered by most recent first.
- Query params:
  - `agent_id` or `session_id` required (at least one). With only `session_id`,
    lists that session's traces across every executing agent.
  - `host_id` optional, filters traces to one host composition
  - `limit` optional, default `5`, clamped to `1..100`
- Returns:
  - `traces`: list of `{ trace_id, session_id, host_id, agent_id, workflow_id, workflow_run_id, started_at, latest_event_at, latest_event_id, event_count }` — `agent_id` is the executing agent; `workflow_id`/`workflow_run_id` are set when the trace was issued by a workflow task
  - `agent_id`, `session_id`, `host_id`
- Errors:
  - `400 INVALID_REQUEST`: neither `agent_id` nor `session_id` was provided

`GET /api/v1/telemetry/usage`
- Time-bucketed usage aggregation for one agent, ordered by bucket ascending.
- Query params:
  - `agent_id` required
  - `host_id` optional
  - `session_id` optional
  - `bucket` optional, `hour` or `day` (default `day`)
  - `from_ts` optional unix-seconds lower bound (inclusive)
  - `to_ts` optional unix-seconds upper bound (exclusive)
- Returns:
  - `buckets`: list of `{ bucket_start, request_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, tool_error_count }`
  - `agent_id`, `host_id`, `session_id`, `bucket`, `from_ts`, `to_ts`
- Errors:
  - `400 INVALID_BUCKET`: `bucket` is not `hour` or `day`

`GET /api/v1/telemetry/trace/analysis`
- Returns span tree and deterministic latency analysis for one trace.
- Query params:
  - `agent_id` required
  - `session_id` required
  - `trace_id` required
- Returns:
  - `analysis`: timing breakdown, tool stats, step breakdown, slowest operations, subagent traces
  - `span_tree`: hierarchical span tree with kind, name, duration, children, and attributes
  - `status`, `total_duration_ms`
  - `tokens`: `{ input_tokens, output_tokens, cache_read_tokens, cache_write_tokens }`
  - `counts`: `{ step_count, tool_call_count, tool_error_count, event_count }`
- Errors:
  - `404 TRACE_NOT_FOUND`: no events found for the given trace

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
- The admin dashboard SPA routes live in [admin_ui.py](/Users/sid/Projects/mashpy/src/mash/api/admin_ui.py).
- The default host config lives in [config.py](/Users/sid/Projects/mashpy/src/mash/api/config.py).

## Verification Notes
- Route inventory cross-checked against `create_app(...).openapi()` plus the non-schema routes mounted on the FastAPI app.
- `http://127.0.0.1:8000/openapi.json` was not available during this update, so the live endpoint could not be fetched; the README was derived from the local app registration instead.

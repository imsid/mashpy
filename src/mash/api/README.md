# API

Lightweight reference for the HTTP surface composed by `src/mash/api/app.py`
and implemented in `src/mash/api/routes/`.

This README lists the exposed routes, the common request/response conventions, and the main query/body fields without duplicating full OpenAPI schemas.

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
- `/h2a/*` and `/sandbox/*` are the relays agent processes use for subagent calls and the sandbox tools. They do not use the API key: each call carries the caller's agent token, derived from `MASH_HOST_SECRET` and the agent id. Without a host secret they return `503 H2A_UNAVAILABLE` and `503 SANDBOX_UNAVAILABLE`.
- `/s/<link token>/*` are users' links into their sandbox (see Sandbox Links). They do not use the API key: the link token names the user and only the host can make one.
- `/browser/*` is the relay agent processes use for the browser tools, with the agent token like `/sandbox/*`. `/s/<view token>/browser/*` are users' live views of their browser during a sign-in (see Browser Live Views); the view token names the user and the request, and lapses when the handoff ends.

## H2A Relay

Raw H2A (no `{data}` envelope), so an agent process calls it with a plain `AgentClient` at `{host}/h2a`.

`POST /h2a/agent/{subagent_id}/request`
- Body: an H2A request (section 6.1 of the RFC) with `parent.kind = "subagent"` and the request's `host` snapshot.
- `401 UNAUTHORIZED` for a token that matches no agent; `403 FORBIDDEN` when the caller is not `parent.agent_id` or `subagent_id` is not in `host.subagents`.
- Forwards to the subagent's client and returns `202` with the accepted payload.

`GET /h2a/agent/{subagent_id}/request/{request_id}`
- Streams the subagent request's events as SSE. Only the agent that submitted it (its `parent.agent_id`) may stream it; others get `403`.

## Sandbox Relay

Every relay call names the request it serves. The host reads the request's identity: an unknown request returns `404 REQUEST_NOT_FOUND`, another agent's request `403 FORBIDDEN`. It acts in the sandbox of the request's user, in the caller's folder, starting the sandbox if needed. `503 SANDBOX_UNAVAILABLE` when the host has no sandboxes or one fails to start; `503 SANDBOX_CAPACITY` when `MASH_SANDBOX_MAX_RUNNING` sandboxes are all busy. No `{data}` envelope.

`POST /sandbox/exec`
- Body: `{request_id, command, timeout, stdin?}`; `timeout` defaults to 30 seconds. Sent by `run_code` with the agent's token.
- Runs the command with `stdin` as its input and returns `{output, exit_code, timed_out}`. The command sees `MASH_FILES_URL` (the user's link to the caller's folder) and `MASH_PORTS_URL`.

`PUT /sandbox/files?request_id=&path=` and `GET /sandbox/files?request_id=&path=`
- Write the body to, or read, one file at `path` in the caller's folder; sent by `edit_file`, `sandbox_put` and `sandbox_get`. `path` must stay in the folder (`400` otherwise); a missing file is `404 FILE_NOT_FOUND`; files are limited to 100 MB (`413`).

## Sandbox Links

Links open in a browser with no API key. `<link token>` is `<base64url(user_id)>.<hmac>`, derived from `MASH_HOST_SECRET`; a token the host did not make is `404`. A link starts a stopped sandbox. Responses carry `Content-Security-Policy: sandbox allow-scripts ...`, so a page the model wrote runs scripts but cannot use the host's origin.

`GET /s/<link token>/files/<agent_id>/<path>`
- Serves the file from that agent's folder of the user's sandbox, with a content type from its extension. A path ending in `/` serves its `index.html`.

`/s/<link token>/ports/<n>/<path>` (any method)
- Forwards HTTP to a server listening on port `n` in the user's sandbox, through the sandbox daemon. `/s/<link token>/ports/<n>` redirects to the trailing slash, so pages can use relative URLs. `502 PORT_UNREACHABLE` when nothing listens. Responses are buffered; WebSockets are not forwarded.

Commands get the bases as `MASH_FILES_URL` and `MASH_PORTS_URL`, under `MASH_PUBLIC_URL` (default: the bind address).

## Browser Relay

Every call names the request it serves, and the host checks it as the sandbox relay does: `404 REQUEST_NOT_FOUND` for an unknown request, `403 FORBIDDEN` for another agent's request or an agent whose spec lists no `browser_sites()`. It acts in the browser of the request's user, on the caller's sites, starting the browser if needed. `503 BROWSER_UNAVAILABLE` when the host has no browsers or one fails to start; `503 BROWSER_CAPACITY` when `MASH_BROWSER_MAX_RUNNING` browsers are all busy. No `{data}` envelope. Results are `{content, images, page_url, is_error}`, where `images` are `{media_type, data}` with base64 data.

`POST /browser/call`
- Body: `{request_id, tool, arguments}`. `tool` is one of the relayed Playwright MCP tools (`mash.browser.catalog.RELAYED_TOOLS`); another name is `400 UNKNOWN_TOOL`.
- Runs it in the request's own tab. A URL outside the caller's sites, or a page a call lands on outside them, comes back as an error result and the page is not shown.

`POST /browser/save`
- Body: `{request_id, download_id, path}`. Copies a finished download into `path` in the caller's sandbox folder; sent by `browser_save`.

`POST /browser/upload`
- Body: `{request_id, path, target, element?}`. Reads `path` from the caller's sandbox folder, clicks `target` to open the file chooser, and picks the file; sent by `browser_upload`.

`POST /browser/handoff` and `POST /browser/handoff/end`
- Body: `{request_id}`. Start returns `{url, expires_at, timeout_seconds}`, the user's live-view link; end revokes it and returns the page. The runtime sends both around a `browser` interaction.

## Browser Live Views

`<view token>` is `<base64url(claims)>.<hmac>` over the user, the request and an expiry (`MASH_BROWSER_HANDOFF_SECONDS`, default 900), derived from `MASH_HOST_SECRET`. A request's token works while its handoff is open; one made by `mash browser view`, for no request, works until it expires. Otherwise `404`.

`GET /s/<view token>/browser/`
- Redirects to noVNC's client, `vnc.html`, set to connect to the websocket below.

`GET /s/<view token>/browser/<path>`
- Serves noVNC's client files from the user's browser.

`WS /s/<view token>/browser/websockify`
- Bridges noVNC to the VNC server on the display Chromium draws to. No `frame-ancestors` restriction, so an app can embed the view.

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
  "user_id": "required: the end user the request acts for",
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
- Lists pooled agents, each with `enabled` and `status` (`running`, `starting`, `unavailable`, `disabled`).
- Returns `agents` and `hosts`.

`POST /api/v1/agent/{agent_id}/enable`
- Starts the agent's process, waits for its health check, and accepts requests for it again. Requests left pending when it was disabled continue.
- Returns `agent`. `503 AGENT_UNAVAILABLE` when the process does not start.

`POST /api/v1/agent/{agent_id}/disable`
- Stops accepting requests for the agent and stops its process. Requests in flight are not cancelled; they stay pending until the agent is enabled.
- `409 AGENT_IN_USE`, with `details.references`, while a host or workflow names the agent.
- Returns `agent`. The enabled set is in memory and resets on restart.

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
- Body: `HostSubmitRequest` (`message`, `session_id`, `user_id`, optional
  `structured_output`, optional `context`, optional `metadata`)
- `user_id` is required: the end user the request acts for. It is stored on the
  request and its turns, never reaches the prompt, and subagent calls inherit
  it. The request's code runs in that user's sandbox. A missing value
  fails validation with `422`; an invalid one returns `400 INVALID_USER_ID`.
- `context` is a freeform string the host appends to the primary agent's
  system prompt for this request only (after the subagent routing block).
  Use it to pass per-request/session facts like user profile, workspace
  state, or the current date.
- `metadata` is an opaque JSON object for code, not the model: it never
  reaches the prompt. It is stored on the request (a crash-replayed request
  reruns with the same metadata), bound for the duration of the run so tools
  can read it via `mash.logging.get_request_metadata()`, and inherited by
  subagent requests made through `InvokeSubagent`.
- Returns `request_id`, `agent_id` (the primary), `session_id`, and
  `user_id`. Stream
  results from `GET /api/v1/agent/{agent_id}/request/{request_id}/events`.

`GET /api/v1/agent/{agent_id}`
- Returns agent metadata plus current session info; `session` is null for a disabled agent.
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
- Optionally accepts a `metadata` JSON object — opaque caller context that
  never reaches the model. Readable during execution via
  `mash.logging.get_request_metadata()` and inherited by subagent requests.
- Requires a `user_id`, as on the host route.
- A disabled agent returns `409 AGENT_DISABLED` here and on its other request routes.

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
- Terminates when event name is `request.completed`, `request.error`, or
  `request.cancelled`.

`POST /api/v1/agent/{agent_id}/request/{request_id}/interaction`
- Answers an interaction the request is waiting on. Body: `{interaction_id, response}`.
- Interactions arrive on the event stream as `request.interaction.create` with
  `interaction_id`, `type`, `prompt`, `schema` and `timeout_seconds`:
  - `info`: `response` is text. `choice`: a list of the options picked.
  - `approval`: `approve`, `deny` or `skip`, for an approval-gated tool.
  - `browser`: a sign-in handoff from `browser_handoff`. The payload adds `url`,
    the user's live view of their browser, and `expires_at` (unix seconds).
    Show the user the link (a button that opens it, or the view in an iframe),
    then answer `done` when they finish or `cancel`.
- `request.interaction.ack` follows with the `response`, plus `timed_out` or
  `cancelled` when set.

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
- Rejected with `409 REQUEST_STALE` when the request's session has accrued a
  newer replayable turn (resume replays the original context snapshot, so it is
  unsafe once the session has moved on).
- On a successful resume the runtime appends a non-terminal
  `runtime.request.resumed` event (SSE `request.resumed`). Since terminality
  reads the last event, this flips a previously failed/cancelled request back to
  non-terminal, so a client whose stream had ended re-opens it after resuming.

`POST /api/v1/agent/{agent_id}/request/{request_id}/cancel`
- Cancels a running request. The in-flight step finishes and checkpoints;
  execution stops at the next step boundary. A request parked on an
  interaction has that prompt acked as cancelled, and the cancel path emits the
  terminal `runtime.request.cancelled` event (visible as `request.cancelled` on
  the SSE stream) so streams terminate. Cancel never cascades to subagent
  children — an already-invoked child runs to completion.
- Path params:
  - `agent_id`
  - `request_id`
- Returns:
  - `request_id`
  - `workflow_id`
  - `status`: `cancelled` | current status when already terminal
  - `message`: human-readable description
- Cancelling an already-terminal request is an idempotent no-op that reports the
  current state. Resume the cancelled request to continue from its last
  checkpoint.

`POST /api/v1/agent/{agent_id}/request/{request_id}/rerun`
- Starts a previous request over as a brand-new request. Rebuilds from the
  original `request.accepted` payload (message + full request metadata, including
  the host snapshot and structured-output request), stamps `rerun_of` provenance,
  and submits through the normal path — new request id, new trace, and a fresh
  `context.load` against the session's current history. Works for any previous
  request regardless of its terminal state; the original is left untouched.
- Path params:
  - `agent_id`
  - `request_id`
- Returns the new request's accepted payload (`request_id`, `agent_id`,
  `session_id`, `status`). Stream the new `request_id` as usual.

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
- Returns catalog summaries with `workflow_id`, display fields, step counts,
  a step preview, history availability, and the latest stored run.

`GET /api/v1/workflow/{workflow_id}`
- Returns the complete registered definition: workflow input schema, metadata,
  and ordered step schemas and execution details.

`POST /api/v1/workflow/{workflow_id}/run`
- Starts a workflow run.
- Path params:
  - `workflow_id`
- Body fields:
  - `dedup_key` optional string
  - `input` optional JSON object
  - `session_id` optional; the run executes under this session (e.g. the REPL
    session) so it is a trace within it. Absent → a fresh per-run session.
  - `user_id`: the user the run acts for. Required when the workflow has an
    `AgentStep` or a `CodeStep` that declares `agent_ids`
    (`400 USER_ID_REQUIRED` otherwise); `400 INVALID_USER_ID` when malformed.
    Every agent step submits for this user, and a resumed run keeps it.
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
- Returns one workflow run with immutable `workflow_input`, `session_id`, `user_id`, final
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

Feedback is stored in the `runtime_feedback` table next to the runtime event log.

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
data model). These endpoints
require a configured `MASH_DATABASE_URL`; without one every route returns `503
EVALS_NOT_AVAILABLE`. Generation and scoring are not HTTP-native: they run as
the `gen-synthetic-evals` and `run-experiment` workflows through the normal
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

### Cortex

Mounted only when the pool calls `HostBuilder.enable_cortex()`. Entries an app
creates or edits are pinned (`origin: app`); `cortex-build` never changes or
deletes them. See `src/mash/agents/cortex/README.md`.

`GET /api/v1/cortex/users/{user_id}`
- Returns `user_id`, `profile`, `built_through`, `version`, `entry_count`, `updated_at`.

`GET /api/v1/cortex/users/{user_id}/entries`
- Query: optional `q` (every term in topic and content), `origin` (`build` or
  `app`), `limit` (1-200, default 50), `after` (the previous page's `next`).
- Returns `entries` newest first and `next`.

`POST /api/v1/cortex/users/{user_id}/entries`
- Body: `topic`, `content`. Creates an `app` entry, and the user's row if missing.
- Returns `entry` and the user's new `version`.

`PATCH /api/v1/cortex/users/{user_id}/entries/{entry_id}`
- Body: `topic` and/or `content`. The entry becomes `app`.
- Returns `entry` and `version`.

`DELETE /api/v1/cortex/users/{user_id}/entries/{entry_id}`
- Returns `deleted` and `version`.

`DELETE /api/v1/cortex/users/{user_id}`
- Deletes the user's profile and entries and resets the build watermark.

Errors: `404 CORTEX_NOT_FOUND` for an unknown user or entry, `400 INVALID_USER_ID`.

### Browsers

Present on every host; `503 BROWSER_UNAVAILABLE` when it has no browsers.

`GET /api/v1/browser/users/{user_id}/sites`
- Returns `{user_id, sites}`: each `{domain, cookies}` the user's browser
  profile holds cookies for, which is where the user is signed in. Starts the
  browser if it is stopped.

`DELETE /api/v1/browser/users/{user_id}/sites/{domain}`
- Signs the user out of `domain` and its subdomains: deletes their cookies and
  clears their storage. Returns `{user_id, domain, cookies}` with the number
  deleted.

`DELETE /api/v1/browser/users/{user_id}`
- Stops the user's browser and deletes its volume, profile included. Returns
  `{user_id, deleted: true}`.

### Observability

Memory search uses the target agent's `memory_store`.

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
- `503 AGENT_UNAVAILABLE`: the agent's server could not be reached, for example because its process is restarting
- `409 AGENT_DISABLED`: the agent is disabled
- `409 AGENT_IN_USE`: disabling an agent a host or workflow names
- `503 SESSIONS_UNAVAILABLE`: session reads for an agent that overrides `build_memory_store()`; the host reads sessions from the pool's shared memory store only
- `400 INVALID_REQUEST`: blank required path/body values after trimming
- `503 MEMORY_SEARCH_UNAVAILABLE`: memory search unavailable for the target agent
- `400 SEARCH_VALIDATION_ERROR`: invalid memory search arguments
- `503 SEARCH_UNAVAILABLE`: search backend not available
- `500 SEARCH_FAILED`: unexpected memory search failure
- `400 INVALID_STRUCTURED_OUTPUT`: `structured_output` is neither a dict nor a Pydantic-serialized schema
- `400 INVALID_AGENT_WORKFLOW`: workflow validation failed (duplicate task ids, missing `task_message` fields, agent not registered, etc.)
- `404 REQUEST_NOT_FOUND`: unknown `request_id` for status, resume, or cancel operations
- `409 REQUEST_STALE`: resume rejected because the request's session has newer replayable turns

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

## Admin Thoughts

`GET /api/v1/telemetry/thoughts?agent_id=pilot` returns the latest 50 traces
containing nonempty exposed thought summaries. Omit `agent_id` for all agents.
The limit counts qualifying traces, not individual summaries; each selected
trace includes all its original summaries in chronological order. Unknown agents
return 404. Optional `trace_id` filtering applies before the limit so direct
links can retrieve older traces. The response echoes `agent_id` and `trace_id`.
Capture is always enabled for supported providers. Reading the feed does not
invoke a model.

The normal API authentication protects this endpoint and the bundled expression
assets at `GET /api/v1/telemetry/thoughts/expressions/{asset}` (catalog GIF/PNG
names only). Admin's Thoughts tab uses the same cookie as Logs and links back
to each exact trace; the Logs trace sidebar links back to its thoughts. The former generation POST and public `/mushy/{id}` routes
are removed. Thoughts reuses runtime events; migration 003 adds only its query index.

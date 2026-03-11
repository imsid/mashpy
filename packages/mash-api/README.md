# mash-api

OpenAPI service package for self-hosted Mash applications.

## Install

```bash
pip install mash-api
# or
pip install "mashpy[api]"
```

## Usage

```python
from mash_api import create_app
from my_app import definition

app = create_app(definition)
```

Run with Uvicorn:

```bash
uvicorn my_module:app --host 127.0.0.1 --port 8000
```

Or use the bundled CLI:

```bash
mash-api --app my_app:build_definition
```

## Interactions API

The service exposes two related interaction patterns:

- `POST /api/v1/interactions/invoke` for a synchronous call that blocks until a terminal result.
- `POST /api/v1/interactions/requests` for an asynchronous call that returns a `request_id` immediately.

### `POST /api/v1/interactions/invoke` (sync)

Use this when you want one request/one response behavior.

- Waits for completion (or failure) and returns the final payload.
- Accepts `message`, optional `session_id`, optional `turn_metadata`, and optional `timeout_ms`.
- Can return `504` if `timeout_ms` is exceeded.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/interactions/invoke \
  -H "Content-Type: application/json" \
  -d '{"message":"hello","session_id":"s1","timeout_ms":15000}'
```

### `POST /api/v1/interactions/requests` (async)

Use this when you want job-style behavior and event streaming.

- Returns quickly with `{"data":{"request_id":"..."}}`.
- Accepts `message`, optional `session_id`, optional `turn_metadata`.
- Does not accept `timeout_ms`; completion is tracked through events.

Submit:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/interactions/requests \
  -H "Content-Type: application/json" \
  -d '{"message":"hello","session_id":"s1"}'
```

Then stream events:

```bash
curl -N http://127.0.0.1:8000/api/v1/interactions/requests/<request_id>/events
```

SSE terminal events are `request.completed` or `request.error`.

## OpenAPI docs

OpenAPI is available at `/openapi.json` (and Swagger UI at `/docs` if enabled by your FastAPI deployment).
It includes endpoint shapes and schemas, but does not currently encode full behavioral guidance such as:

- `invoke` being a blocking wrapper over submit+stream.
- expected event progression (`request.accepted` -> `request.started` -> terminal event).
- operational tradeoffs between sync and async flows.

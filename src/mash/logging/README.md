# Logging

`src/mash/logging` provides structured log events and trace correlation utilities.

## What This Package Does
- Defines the structured event payloads emitted outside the runtime's own request events.
- Implements the canonical event logger used by hosted agents.
- Provides helpers for creating and propagating trace IDs across agent, tool, and host boundaries.

## Main Components
- `events.py`: structured event types used across Mash.
- `logger.py`: canonical event logger implementation.
- `trace_context.py`: trace, request, session, loop-step and subagent-prefix context helpers.

## Role In The System
- A request's own events (`invoke_agent.*`, `chat.*`, `execute_tool.*`, `mash.*`) are
  written by the durable runtime in `runtime/engine/steps.py` and
  `runtime/requests.py`; see `runtime/events/types.py` for the event set.
- This package writes the rest: CLI command events, MCP connection events, and the
  `chat.delta` chunks a provider streams.
- Event shapes should remain machine-readable and stable enough for downstream inspection and tests.

## Logged Event Stream
- `EventLogger` persists structured events through the canonical runtime event store.
- Hosted events live in Postgres `runtime_event_log`.
- Downstream readers should treat each stored row as an independent event record.
- Every record includes an `event_type` string plus request/session/trace context when available.
- Inside a request, the logger stamps the bound request id and loop step on the row,
  and stores the event's `dedupe_key`, so a replayed emit writes nothing new.
- The concrete event classes currently written are:
  - `CommandEvent`
  - `MCPEvent`
  - `LLMEvent`

## Common Record Shape
All logged events inherit this base shape:

```json
{
  "event_type": "string",
  "ts": 1710000000.123,
  "app_id": "string",
  "session_id": "string or null",
  "event_class": "LogEvent subclass name",
  "payload": {}
}
```

Base field notes:
- `event_type`: stable event name used for routing and filtering.
- `ts`: Unix timestamp in seconds.
- `app_id`: application identifier for the host/agent/app that emitted the event.
- `session_id`: conversation or runtime session when available.
- `event_class`: one of the concrete dataclass names above.
- `payload`: event-specific structured data.
- `dedupe_key` (optional, not serialized): stored on the row inside a request.

Storage notes:
- SQLite stores the common envelope in top-level `logs` columns:
  `app_id`, `session_id`, `trace_id`, `event_class`, `event_type`, `created_at`.
- Remaining event fields are stored in the row's JSON `payload` column and reconstructed back into the current public event shape on reads.

## Lightweight Schema By Event Class

`CommandEvent`
- Adds:
  - `command_name`
  - `args`
  - `duration_ms`
  - `error`
  - `trace_id`

`MCPEvent`
- Adds:
  - `server_name`
  - `server_url`
  - `tool_name`
  - `duration_ms`
  - `error`
  - `metadata`
  - `trace_id`

`LLMEvent`
- Adds:
  - `provider`
  - `model`
  - `duration_ms`
  - `input_tokens`
  - `output_tokens`
  - `total_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  - `finish_reason`
  - `error`
  - `metadata`
  - `trace_id`
  - `tools`
  - `betas`

## Event Types Currently Emitted

### Command Lifecycle
- `command.start`
- `command.complete`
- `command.error`

Typical fields:
- `command_name`
- `args`
- `duration_ms`
- `error`

### Streamed Model Output
- `chat.delta`

Notes:
- Emitted only for streaming requests (`request.streaming`), while the model call runs.
- Carries a coalesced text chunk in `payload` as `{"text", "index"}`; chunks are
  coalesced by the provider so volume stays bounded (tens per turn).
- Inside a request its dedupe key is `chat.delta.{loop_index}.{index}`, so a replayed
  model call keeps the first attempt's chunks.
- The model call's outcome, token counts, provider and model are on the request's
  `chat.end`.

### MCP Client Events
- `mcp.client.connect`
- `mcp.client.connected`
- `mcp.client.disconnect`
- `mcp.client.error`

Typical fields:
- `server_name`
- `server_url`
- `error`
- `trace_id`

Typical metadata:
- `mcp.client.connected` may include `tool_count`

An MCP tool call is the request's `execute_tool` span; the manager writes no event for it.

## Pilot-Agent Notes
- Treat `event_type` as the primary key for classification.
- Treat `event_class` as the shape discriminator for top-level optional fields.
- Expect sparse records: many top-level optional fields are `null`.
- `trace_id` and `session_id` are the main correlation keys across related events.
- For storage-level debugging, inspect the agent memory store's logs table rather than looking for a standalone JSONL log file.

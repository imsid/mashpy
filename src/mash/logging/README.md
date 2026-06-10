# Logging

`src/mash/logging` provides structured runtime events and trace correlation utilities.

## What This Package Does
- Defines the structured event payloads emitted across the system.
- Implements the canonical runtime-event logger used by hosted agents.
- Provides helpers for creating and propagating trace IDs across agent, tool, and host boundaries.

## Main Components
- `events.py`: structured event types used across Mash.
- `logger.py`: canonical runtime-event logger implementation.
- `trace_context.py`: trace ID helpers.

## Role In The System
- Logging is shared by core execution, runtime hosting, tools, and subagent orchestration.
- Event shapes should remain machine-readable and stable enough for downstream inspection and tests.

## Logged Event Stream
- `EventLogger` persists structured events through the canonical runtime event store.
- Hosted observability events live in Postgres `runtime_event_log`.
- Downstream readers should treat each stored row as an independent event record.
- Every record includes an `event_type` string plus request/session/trace context when available.
- The concrete event classes currently written are:
  - `CommandEvent`
  - `AgentTraceEvent`
  - `MCPEvent`
  - `LLMEvent`
  - `MemorySearchEvent`
  - `DebugEvent`

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
- `payload`: event-specific structured data. This is often empty for lifecycle-only events and populated for previews, token breakdowns, or custom metadata.

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

`AgentTraceEvent`
- Adds:
  - `trace_id`
  - `step_id`
  - `duration_ms`
  - `action_type`
  - `tool_calls`
  - `skill_calls`
  - `token_usage`

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

`MemorySearchEvent`
- Adds:
  - `query_id`
  - `level`
  - `stage`
  - `duration_ms`
  - `error`
  - `metadata`

`DebugEvent`
- Adds:
  - `message`
  - `exception_type`
  - `exception_message`
  - `stack_trace`
  - `context`

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

### Agent Execution
- `agent.run.start`
- `agent.think.start`
- `agent.think.complete`
- `agent.act.complete`
- `agent.tool.call`
- `agent.tool.result`
- `agent.step.complete`
- `agent.run.complete`
- `agent.compaction`

Typical fields:
- `trace_id`
- `step_id`
- `duration_ms`
- `action_type`
- `tool_calls`

Typical payload content:
- `agent.run.start`: `user_message`
- `agent.run.complete`: `assistant_response`
- `agent.think.complete`: `tool_calls_detail`, truncated `assistant_text`
- `agent.tool.call`: `tool_name`, `tool_call_id`, `tool_arguments`
- `agent.tool.result`: `tool_name`, `tool_call_id`, `is_error`, `content_length`, `content_preview`, `metadata`
- `agent.compaction`: `reason`, `summary_turn_id`, token-threshold details

### Mirrored Subagent Events
- `subagent.request.accepted`
- `subagent.request.started`
- `subagent.request.completed`
- `subagent.request.error`
- `subagent.agent.trace`

Notes:
- These are emitted as `AgentTraceEvent` records in the parent trace.
- They mirror child runtime request-stream events so parent traces can correlate delegated work across sessions.

### LLM Provider Lifecycle
- `llm.request.start`
- `llm.response.delta`
- `llm.request.complete`
- `llm.request.error`

Typical fields:
- `provider`
- `model`
- `duration_ms`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `finish_reason`
- `trace_id`
- `tools`
- `betas`

Typical metadata:
- Provider-specific response metadata on `llm.request.complete`

`llm.response.delta` notes:
- Emitted only for streaming requests (`request.streaming`), interleaved
  between `llm.request.start` and `llm.request.complete`.
- Carries a coalesced text chunk in `payload` as `{"text", "index"}` (no
  duration/token fields); chunks are coalesced by the provider so volume stays
  bounded (~tens per turn).
- `llm.request.complete` remains the source of truth for final `duration_ms`
  and token counts.

### MCP Client And Tool Events
- `mcp.client.connect`
- `mcp.client.connected`
- `mcp.client.disconnect`
- `mcp.client.error`
- `mcp.tool.call`
- `mcp.tool.result`
- `mcp.tool.error`

Typical fields:
- `server_name`
- `server_url`
- `tool_name`
- `duration_ms`
- `error`
- `trace_id`

Typical metadata:
- `mcp.client.connected` may include `tool_count`

### Memory Search Pipeline
- `memory.search.start`
- `memory.search.parse.complete`
- `memory.search.parse.error`
- `memory.search.retrieval.complete`
- `memory.search.retrieval.error`
- `memory.search.rerank.complete`
- `memory.search.rerank.error`
- `memory.search.complete`

Typical fields:
- `query_id`
- `level`
- `stage`
- `duration_ms`
- `error`

Stage values currently used:
- `service`
- `parse`
- `retrieval`
- `rerank`

Typical metadata:
- `memory.search.start`: normalized query details, requested/normalized limit, session scope
- `memory.search.parse.complete`: parsed `column`, normalized `query_term`
- `memory.search.retrieval.complete`: hit counts and enabled retrieval modes
- `memory.search.rerank.complete`: fused hit count
- `memory.search.complete`: result count, applied limit, optional short-circuit reason

## Pilot-Agent Notes
- Treat `event_type` as the primary key for classification.
- Treat `event_class` as the shape discriminator for top-level optional fields.
- Expect sparse records: many top-level optional fields are `null`.
- Expect richer details in `payload` and class-specific `metadata`.
- `trace_id`, `session_id`, and `query_id` are the main correlation keys across related events.
- For storage-level debugging, inspect the agent memory store's logs table rather than looking for a standalone JSONL log file.

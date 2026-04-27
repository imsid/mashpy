# Runtime

`src/mash/runtime` contains the hosted execution layer that turns an `AgentSpec` into a running Mash deployment.

## What This Package Does
- Defines the `AgentSpec` contract used to build agents.
- Composes one primary agent and optional subagents into a `MashAgentHost`.
- Runs one in-process uvicorn-managed Starlette server per addressable agent runtime.
- Keeps the execution core transport-free so runtime state, tools, persistence, logs, and MCP integrations live in `MashAgentRuntime`, not in the ASGI adapter.
- Provides runtime client/session contracts used by API and CLI surfaces.

## Main Components
- `spec.py`: `AgentSpec`, the single-agent SDK contract, including `build_memory_store()` and `build_runtime_store()`.
- `runtime.py`: `MashAgentRuntime`, the execution core for one agent.
- `server.py`: `MashAgentServer`, the Starlette transport adapter over one runtime.
- `host.py`: `AgentSpec`-based host composition, in-process uvicorn server startup, and client registry behavior.
- `client.py`: async H2A client for one agent runtime.
- `session.py`: session and deterministic subagent session ID derivation.
- `types.py`: runtime result types, `SubAgentMetadata`, and resolved subagent endpoints.
- `errors.py`: runtime-facing exception types.
- `execution/`: event-sourced hosted execution primitives:
  - `RuntimeEvent`, `RuntimeEventType`, `RuntimeReplayState`
  - `RuntimeStore`, `SQLiteRuntimeStore`
  - `RuntimeWorkflowExecutor`
  - `RuntimeRecoveryManager`

## Typical Flow
1. A caller implements `AgentSpec`.
2. The runtime builds tool, skill, memory, logging, and model dependencies from that spec.
3. `MashAgentHostBuilder` composes the primary agent and optional subagents into one host.
4. The host starts one uvicorn-managed Starlette runtime server per addressable agent and builds one H2A client per runtime.
5. The API layer exposes that host, and the CLI or other clients call it through the host-facing contracts defined here.

## Runtime Shape

The runtime is split into three layers:

- `AgentSpec`
  Application build contract. Supplies tools, skills, stores, model, and runtime config.
- `MashAgentRuntime`
  Async execution core. Owns hosted request lifecycle, per-session serialization, event-sourced execution, persistence orchestration, replay, recovery, and subagent client wiring.
- `MashAgentServer`
  Starlette transport adapter. Exposes one runtime over HTTP + SSE.

The host owns multi-agent composition:

- `MashAgentHostBuilder`
  Registers a primary `AgentSpec` and optional subagent `AgentSpec`s plus host-only metadata.
- `MashAgentHost`
  Starts one `uvicorn.Server` task per registered agent runtime, waits for readiness, and keeps one `MashAgentClient` per runtime.

The runtime persists through two separate stores:

- `memory_store`
  Conversation turns, signals, legacy structured logs, and search-oriented state.
- `runtime_store`
  Append-only hosted execution events in `runtime_event_log`.

## H2A Surface

Each runtime server exposes a minimal H2A HTTP surface:

- `GET /health`
- `POST /agent/{agent_id}/request`
- `GET /agent/{agent_id}/request/{request_id}`

The transport lifecycle is:

- `request.accepted`
- optional `request.waiting`
- `request.started`
- zero or more `agent.trace`
- terminal `request.completed` or `request.error`

`request.waiting` means the request was accepted but is blocked behind another in-flight request for the same `session_id`.
These public events are sourced from the persisted runtime event stream rather than from an in-memory request buffer.

## Execution Model

`MashAgentRuntime` is async-native:

- every accepted request creates an async task immediately
- requests for the same `session_id` are serialized with a per-session `asyncio.Lock`
- different sessions may run concurrently up to the runtime concurrency limit
- hosted execution is driven by `RuntimeWorkflowExecutor`
- runtime state is appended to `runtime_store` as `RuntimeEvent`s
- request progress and terminal state are reconstructed by replaying `runtime_event_log`
- startup recovery is driven by `RuntimeRecoveryManager`, which resumes incomplete persisted requests

Hosted execution advances through replayable runtime workflow steps persisted in `runtime_store`. The runtime request interface is `submit_request(...)` plus `stream_request_events(...)`.

## Important Invariants
- `AgentSpec` is the build boundary for a single agent.
- `memory_store` and `runtime_store` have separate responsibilities and should remain separate.
- Hosted request execution is event-sourced through `RuntimeEvent` in `runtime_store`.
- Public request streaming is derived from persisted runtime events.
- Startup recovery must remain compatible with incomplete hosted requests.
- Subagent session derivation must remain deterministic.
- Host-managed subagent routing metadata must stay separate from `AgentSpec`; specs define the agent, the host defines where and how it is exposed.
- Runtime control and session behavior need to stay compatible with both `mash.api` and `mash.cli`.

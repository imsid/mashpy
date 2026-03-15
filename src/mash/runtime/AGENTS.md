# AGENTS Guide for `src/mash/runtime`

## Scope
Runtime-side architecture for Mash apps: agent spec contracts, server execution, HTTP transport, host/client orchestration, and cross-agent session behavior.

## Invariants
- `AgentSpec` is runtime-only and transport-agnostic.
- `MashAgentServer` owns agent execution state and lifecycle:
  - agent loop execution via `process_user_message(...)`
  - request queue + single worker (single-flight per server)
  - request event buffering/replay
  - runtime event logging + trace fan-out
- `MashAgentHTTPHandler`/`MashAgentHTTPServer` are transport only; business logic stays in `MashAgentServer`.
- HTTP API is the only client/server interaction path:
  - `POST /agents/{agent_id}/requests`
  - `GET /agents/{agent_id}/requests/{request_id}` (SSE)
  - `POST /agents/{agent_id}/control`
  - `GET /agents/{agent_id}/control?action=...`
- `MashAgentHost` owns multi-agent orchestration:
  - register primary/subagents
  - create one `MashAgentClient` per registered agent
  - inject subagent prompt guidance and `InvokeSubagent` wiring on primary
- `MashAgentClient` is a 1:1 client for a single agent id and follows:
  - `POST /agents/{agent_id}/requests`
  - `GET /agents/{agent_id}/requests/{request_id}` SSE
  - `POST/GET /agents/{agent_id}/control` for runtime controls and command-event forwarding
- `MashAgentClient` must not depend on direct/local runtime control objects.
- Subagent session IDs remain deterministic via `derive_subagent_session_id(...)`.

## No-Legacy Rule
- Do not reintroduce legacy IPC/subprocess subagent paths (`mash.ipc`, `serve_ipc`, router/worker wiring).
- Do not add compatibility aliases for removed SDK types or constructors.

## Change Rules
- Keep HTTP event contract stable (`request.accepted`, `request.started`, `agent.trace`, `request.completed`, `request.error`).
- Keep control action contract stable (session info, prefs, app data, history, compaction, command event emit).
- Keep server/host/client responsibilities separated; avoid leaking CLI concerns into runtime modules.
- If request payloads or event shapes change, update:
  - `README.md`
  - runtime/client/host/subagent tests
  - examples using host/client/server flow

## Minimal Validation
- `uv run pytest -q tests/mash/runtime/test_engine.py tests/mash/runtime/test_host_integration.py tests/mash/tools/test_subagent.py`
- `uv run python -m compileall src/mash/runtime`

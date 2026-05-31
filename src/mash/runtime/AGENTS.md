# AGENTS Guide for `src/mash/runtime`

## What Must Stay True
- `AgentSpec` remains the runtime contract for building agents.
- `HostBuilder` composes one primary agent with optional subagents.
- `AgentRuntime` is the per-agent execution core.
- `AgentServer` is the per-agent HTTP/SSE adapter.
- `AgentHost` owns multi-agent composition and lifecycle.
- Hosted request execution is event-sourced through `runtime_store`.
- `RequestEngine` and the `engine/` package remain the workflow/backend boundary.
- `memory_store` and `runtime_store` remain separate responsibilities.
- Runtime servers and clients preserve the current host/session contracts.
- Subagent session derivation remains deterministic.
- Interactions (approval/info/choice) block durably via DBOS `recv`/`send` in the workflow loop.
- `post_interaction` is the only way to deliver a response to a blocked interaction.

## Change Rules
- Keep the root package small. Prefer `engine/`, `events/`, and `host/` for bounded internals.
- Keep host composition, transport, request coordination, and runtime lifecycle logic in this package.
- If hosted request lifecycle, replay semantics, or `RuntimeEventType` changes, update runtime docs and tests together.
- If storage contracts change, update `AgentSpec` docs to reflect the current store responsibilities.
- Preserve `InvokeSubagent` integration and subagent prompt injection behavior used by the host.
- If runtime control APIs or session semantics change, update downstream API and CLI tests together.
- If interaction types or schemas change, update the H2A RFC (`docs/rfcs/host-to-agent-protocol.md`) and client/server together.

## Minimal Validation
- `python -m compileall src/mash/runtime`
- Verify one plain hosted request path, one tool or multi-step loop path, one subagent path, and one session-id path.
- Verify one request-engine path and one replay-oriented event-stream path.
- Verify one interaction path: `request.interaction.create` → `post_interaction` → `request.interaction.ack`.

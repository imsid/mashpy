# AGENTS Guide for `src/mash/runtime`

## What Must Stay True
- `AgentSpec` remains the runtime contract for building agents.
- `HostBuilder` composes a flat pool and hosts of agents and workflows; the built-in companion routes every host request.
- `AgentRuntime` is the per-agent execution core.
- `AgentServer` is the per-agent HTTP/SSE adapter.
- `Pool` owns agent registration, host definitions, and lifecycle.
- Hosted request execution is event-sourced through `runtime_store`.
- `RequestEngine` and the `engine/` package remain the durability boundary; a request's durable state is its `runtime_request` row and its events.
- `memory_store` and `runtime_store` remain separate responsibilities.
- `AgentRuntime` receives stores via constructor injection — it never creates or closes them.
- `Pool` owns shared store instances and their lifecycle; `AgentServer` owns its own.
- Runtime servers and clients preserve the current host/session contracts.
- A `RunSubAgents` child's new session id (its request id, derived from the call's position) remains deterministic.
- Interactions (approval/info/choice/browser) wait in the request loop; the answer is the `mash.interaction.end` event, so a recovered loop finds it.
- Every event a step appends has a dedupe key, so a step that runs twice after a crash appends nothing twice.
- `post_interaction` is the only way to deliver a response to a blocked interaction.

## Change Rules
- Keep the root package small. Prefer `engine/`, `events/`, and `host/` for bounded internals.
- Keep host composition, transport, request coordination, and runtime lifecycle logic in this package.
- If hosted request lifecycle, replay semantics, or `RuntimeEventType` changes, update runtime docs and tests together.
- If storage contracts or store lifecycle ownership change, update `AgentSpec` docs and the runtime README Stores section.
- Preserve the companion's `RunSubAgents`/`RunWorkflow` wiring and its agent directory. Only the companion routes, on host requests the user started. A child request (a `subagent` parent) asks the user through the companion, which relays the interaction onto its own request.
- If runtime control APIs or session semantics change, update downstream API and CLI tests together.
- If interaction types or schemas change, update the H2A RFC (`docs/rfcs/host-to-agent-protocol.md`) and client/server together.

## Minimal Validation
- `python -m compileall src/mash/runtime`
- Verify one plain hosted request path, one tool or multi-step loop path, one companion routing path, and one session-id path.
- Verify one request-engine path and one replay-oriented event-stream path.
- Verify one interaction path: `request.interaction.create` → `post_interaction` → `request.interaction.ack`.

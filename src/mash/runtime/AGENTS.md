# AGENTS Guide for `src/mash/runtime`

## What Must Stay True
- `AgentSpec` remains the runtime contract for building agents.
- `MashAgentHostBuilder` composes one primary agent with optional subagents.
- Hosted request execution is event-sourced through `runtime_store`.
- `RuntimeWorkflowExecutor` and `RuntimeRecoveryManager` remain the hosted execution core.
- `memory_store` and `runtime_store` remain separate responsibilities.
- Runtime servers and clients preserve the current host/session contracts.
- Subagent session derivation remains deterministic.

## Change Rules
- Keep host composition, transport, and runtime lifecycle logic in this package.
- If hosted request lifecycle, replay semantics, or `RuntimeEventType` changes, update runtime docs and tests together.
- If storage contracts change, update `AgentSpec` docs to reflect `build_memory_store()` and `build_runtime_store()`.
- Preserve `InvokeSubagent` integration and subagent prompt injection behavior used by the host.
- If runtime control APIs or session semantics change, update downstream API and CLI tests together.

## Minimal Validation
- `python -m compileall src/mash/runtime`
- Verify one plain hosted request path, one tool or multi-step loop path, one subagent path, and one session-id path.
- Verify one recovery or replay-oriented runtime path if available.

# AGENTS Guide for `src/mash/runtime`

## What Must Stay True
- `AgentSpec` remains the runtime contract for building agents.
- `MashAgentHostBuilder` composes one primary agent with optional subagents.
- Runtime servers and clients preserve the current host/session contracts.
- Subagent session derivation remains deterministic.

## Change Rules
- Keep host composition, transport, and runtime lifecycle logic in this package.
- Preserve `InvokeSubagent` integration and subagent prompt injection behavior used by the host.
- If runtime control APIs or session semantics change, update downstream API and CLI tests together.

## Minimal Validation
- `python -m compileall src/mash/runtime`
- Verify one primary invoke path, one subagent path, and one session-id path.

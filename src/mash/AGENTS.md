# AGENTS Guide for `src/mash`

This package is the Mash codebase root. The SDK/runtime remains the core layer, and the unified distribution also includes hosted API and CLI surfaces under `mash.api` and `mash.cli`.

## What Must Stay True
- `AgentSpec` is the single-agent SDK contract.
- `HostBuilder` composes one primary agent and optional subagents into an `AgentHost`.
- `AgentServer` exposes one per-agent runtime over HTTP + SSE.
- Hosted APIs live in `mash.api`; remote terminal UX lives in `mash.cli`.
- Tool definitions exposed to the model keep the expected schema contract.
- Event logs remain machine-parseable JSONL.

## Cross-Cutting Change Rules
- Keep core SDK/runtime concerns separate from API and CLI surfaces.
- If public exports, runtime defaults, or host contracts change, update the top-level repo docs and affected module docs.
- Preserve trace correlation when emitting events across runtime, tools, logging, and hosted surfaces.

## Minimal Validation
- `python -m compileall src/mash`
- Verify one direct invoke path, one subagent path, and one session/history path.

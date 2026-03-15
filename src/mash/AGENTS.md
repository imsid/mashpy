# AGENTS Guide for `src/mash`

This package is the Mash codebase root. The SDK/runtime remains the core layer, and the unified distribution also includes hosted API and CLI surfaces under `mash.api` and `mash.cli`.

## What Must Stay True
- `AgentSpec` is the single-agent SDK contract.
- `MashAgentHostBuilder` composes one primary agent and optional subagents into a `MashAgentHost`.
- `MashAgentServer` wires the runtime (agent, tools, memory, logging, optional MCP).
- Hosted APIs live in `mash.api`; remote terminal UX lives in `mash.cli`.
- `Agent` runs a think-act-observe loop and records trace/token metadata in response metadata.
- Tool definitions exposed to the LLM must use Anthropic-compatible schema (`name`, `description`, `input_schema`).
- Event logs are JSONL and remain machine-parseable (`LogEvent.to_dict()` output per line).

## Cross-Cutting Change Rules
- Keep SDK, host service, and CLI responsibilities separate even though they now ship in one package.
- If you change defaults or runtime contracts, update the top-level `README.md`.
- Keep trace correlation intact: use `set_trace_id` / `get_trace_id` when emitting cross-component events.

## Minimal Validation
- `python -m compileall src/mash`
- Verify one invoke path, one subagent path, and one session/history path.

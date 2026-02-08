# AGENTS Guide for `src/mash`

This package is a framework for building agent-powered CLI apps.

## What Must Stay True
- `MashApp` wires the runtime (agent, tools, memory, logging, optional MCP) and owns the REPL lifecycle.
- `Agent` runs a think-act-observe loop and records trace/token metadata in response metadata.
- Tool definitions exposed to the LLM must use Anthropic-compatible schema (`name`, `description`, `input_schema`).
- Event logs are JSONL and should remain machine-parseable (`LogEvent.to_dict()` output per line).
- Runtime memory behavior must stay deterministic: stored turns in order, summary checkpoints recognized by metadata type `summary_checkpoint`.

## Cross-Cutting Change Rules
- Preserve backward-compatible command and tool names unless there is an explicit migration.
- If you change defaults (commands, runtime tools, config flags, telemetry output), update `README.md`.
- Keep trace correlation intact: use `set_trace_id` / `get_trace_id` when emitting cross-component events.
- Prefer additive changes over broad rewrites; many modules are intentionally loosely coupled.

## Minimal Validation
- `python -m compileall src/mash`
- If command behavior changed, manually verify one REPL loop path and one slash-command path.

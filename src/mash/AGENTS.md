# AGENTS Guide for `src/mash`

This package is a framework for building agent-powered applications with a composed runtime model.

## What Must Stay True
- `MashAgentServer` wires the runtime (agent, tools, memory, logging, optional MCP).
- Interactive shell ownership lives in the companion `mash-cli` package (`mash_cli.CLIAppShell`).
- `Agent` runs a think-act-observe loop and records trace/token metadata in response metadata.
- Tool definitions exposed to the LLM must use Anthropic-compatible schema (`name`, `description`, `input_schema`).
- Event logs are JSONL and remain machine-parseable (`LogEvent.to_dict()` output per line).
- Runtime memory behavior stays deterministic: stored turns in order, summary checkpoints recognized by metadata type `summary_checkpoint`.

## Cross-Cutting Change Rules
- Preserve command and tool names unless explicitly migrating.
- If you change defaults (commands, runtime tools, config flags, telemetry output), update `README.md`.
- Keep trace correlation intact: use `set_trace_id` / `get_trace_id` when emitting cross-component events.
- Prefer additive changes over broad rewrites; modules are intentionally loosely coupled.

## Minimal Validation
- `python -m compileall src/mash`
- If runtime behavior changed, manually verify one end-to-end invoke path and one memory/logging path.

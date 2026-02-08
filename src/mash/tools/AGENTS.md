# AGENTS Guide for `src/mash/tools`

## Scope
Tool protocol and concrete tool implementations (local bash, runtime memory tools, MCP adapters).

## Invariants
- Every tool must expose:
  - `name`
  - `description`
  - JSON-schema `parameters`
  - `execute(args) -> ToolResult`
  - `to_llm_format()` with `input_schema`
- `ToolRegistry` enforces unique names.
- `ToolResult.is_error` indicates tool-level failure, not shell exit status semantics.

## Bash Tool
- Keep safety checks and root-find protection (`find / ...`) intact.
- Output truncation limits are intentional to control token usage.
- Persistent bash session behavior should survive command errors and restart on timeout.

## Runtime Tools
- `RuntimeToolBuilder.build_tools()` controls which tools are auto-available to apps.
- If adding/removing runtime tools, update docs and verify any app assumptions.
- Store access should remain app/session-scoped through the provided store API.

## MCP Adapter
- Preserve name/input schema mapping from MCP tool definitions (`inputSchema` -> `parameters`).

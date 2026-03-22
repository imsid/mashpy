# Tools

`src/mash/tools` contains the built-in tools exposed to Mash agents.

## What This Package Does
- Defines the base tool contract and registration surface.
- Houses built-in tools such as bash access, MCP-backed tools, and subagent invocation.
- Provides runtime-facing helpers so tool execution can stay consistent across hosted agents.

## Main Components
- `base.py`: tool interface and shared behavior.
- `registry.py`: registration and lookup of enabled tools.
- `bash.py`: repository and terminal inspection tool.
- `mcp.py`: MCP-backed tool integration.
- `subagent.py`: `InvokeSubagent`, used by primary agents to call registered subagents.
- `runtime.py`: runtime-facing helpers for tool execution.

## Role In The System
- `core` and `runtime` rely on this package for the model-visible tool surface.
- Tool schemas should stay stable and predictable for prompts, execution, and tests.

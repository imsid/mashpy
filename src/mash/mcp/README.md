# MCP

`src/mash/mcp` contains Mash's Model Context Protocol integration layer.

## What This Package Does
- Defines the typed MCP configuration surface used by Mash agents.
- Manages MCP server lifecycle and coordination.
- Provides client/server integration code used by runtime and tool layers.
- Keeps protocol-specific logic isolated behind one package boundary.

## Main Components
- `types.py`: canonical typed MCP config structures.
- `manager.py`: MCP manager lifecycle and server coordination.
- `client.py`: client-side MCP communication helpers.
- `server.py`: server behavior and protocol handling.
- `host.py`: host-facing integration helpers.

## Role In The System
- `runtime` and `tools` should consume MCP behavior through this package.
- Protocol details should not be reimplemented in unrelated modules.

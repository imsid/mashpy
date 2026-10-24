# MCP

`src/mash/mcp` contains Mash's Model Context Protocol integration layer.

## What This Package Does
- Defines the typed MCP configuration surface used by Mash agents.
- Manages MCP server lifecycle and coordination.
- Provides client/server integration code used by runtime and tool layers.
- Keeps protocol-specific logic isolated behind one package boundary.
- Supports the core MCP surfaces Mash uses today: tools, resources, prompts, and elicitation.
- Does not implement deprecated MCP features: Roots (`roots/list`, `notifications/roots/list_changed`), Sampling (`sampling/createMessage`), or protocol-level Logging (`logging/setLevel`, `notifications/message`).

## Main Components
- `types.py`: canonical typed MCP config structures.
- `manager.py`: MCP manager lifecycle and server coordination.
- `client.py`: client-side MCP communication helpers.
- `server.py`: server behavior and protocol handling.
- `host.py`: host-facing integration helpers.

## Role In The System
- `runtime` and `tools` should consume MCP behavior through this package.
- Protocol details should not be reimplemented in unrelated modules.
- Deprecated MCP features should remain unsupported unless there is a specific backward-compatibility requirement and an explicit design decision to reintroduce them.

## Where Connections Run
- `build_mcp_servers()` runs in the agent's process, inside `mash agent serve`, a child of the host. Every MCP connection and tool call goes from there to the MCP server; the host process opens none.
- Header tokens stay in the agent process's environment. Agents' code runs in the user's sandbox, which never sees them.
- URLs resolve where the host runs: `localhost` in a server URL is the host's machine or container. A deployment that restricts outbound traffic allows the MCP server hosts on the platform.
- MCP session ids (`MCP-Session-ID`) live in the agent process, so a restarted agent opens new sessions.
- Approval-gated MCP tools pause the request through H2A interactions, as any other tool does. `web_search` and `web_fetch` take the same path.

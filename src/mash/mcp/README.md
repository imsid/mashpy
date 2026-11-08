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
- `manager.py`: connects each server, keeps its tool list, routes tool calls, and answers elicitation requests.
- `client.py`: the async MCP HTTP client.
- `server.py`: one connected client and its `allowed_tools` filter.

## Role In The System
- `runtime` and `tools` should consume MCP behavior through this package.
- Protocol details should not be reimplemented in unrelated modules.
- Deprecated MCP features should remain unsupported unless there is a specific backward-compatibility requirement and an explicit design decision to reintroduce them.

## Where Connections Run
- `build_mcp_servers()` runs in the agent's process, inside `mash agent serve`, a child of the host. Every MCP connection and tool call goes from there to the MCP server; the host process opens none.
- Header tokens stay in the agent process's environment. Agents' code runs in the user's sandbox, which never sees them.
- URLs resolve where the host runs: `localhost` in a server URL is the host's machine or container. A deployment that restricts outbound traffic allows the MCP server hosts on the platform.
- MCP session ids (`MCP-Session-ID`) live in the agent process, so a restarted agent opens new sessions.

## Connection Lifecycle
- `AgentRuntime.open()` connects each server from `build_mcp_servers()`, plus the web search provider's, and lists its tools once. `MCPManager` keeps the lists for the life of the process; building an agent for a step reads them without a network call.
- A server that fails to connect or list its tools is logged as a warning (`mash.mcp.manager`) and left out. The agent starts without its tools, and the other servers still connect. Restarting the agent tries again.
- `MCPHTTPClient` is async, on `httpx.AsyncClient`. Tool calls run on the agent's event loop without blocking it, so parallel calls overlap. The SSE listener for server-initiated messages is a task on the same loop.
- Each RPC logs one DEBUG line on `mash.mcp.client`: the server name, the method, the HTTP status and the duration. Params and response bodies are not logged.
- Approval-gated MCP tools pause the request through H2A interactions, as any other tool does. `web_search` and `web_fetch` take the same path.

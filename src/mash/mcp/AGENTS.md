# AGENTS Guide for `src/mash/mcp`

## Scope
MCP transport + orchestration: HTTP client, host, server wrapper, and multi-server manager.

## Invariants
- MCP HTTP init flow requires:
  1) `initialize`
  2) session header capture (`MCP-Session-ID`)
  3) `notifications/initialized`
- URL normalization must preserve the `/mcp/` endpoint expectation.
- Manager/server APIs should fail clearly when disconnected.

## Tool Routing
- `MCPManager.get_flattened_tools()` prefixes tool names and stores original server metadata.
- `MCPServer.allowed_tools` is a whitelist; keep checks strict in `list_tools()` and `call_tool()`.

## Interactions
- Sampling requests are handled by `Host` via OpenAI chat completions.
- Elicitation requests are interactive and currently use terminal input.
- Preserve async boundaries in client interaction handlers.

## Logging
- Emit `MCPEvent` for connect/call/result/error paths.
- Use `get_trace_id()` to correlate MCP events with agent traces.

# AGENTS Guide for `src/mash/mcp`

## What Must Stay True
- MCP client, server, manager, and configuration behavior stay in this package.
- MCP types remain the canonical typed surface for protocol configuration.
- Runtime and tool integrations should consume MCP behavior through this module boundary.
- Supported MCP behavior is limited to the surfaces Mash actively uses: tools, resources, prompts, and elicitation.
- Deprecated MCP Roots, Sampling, and protocol Logging remain unsupported unless a future compatibility decision explicitly changes that.

## Change Rules
- Keep MCP-specific logic in `mash.mcp`; avoid leaking protocol details into unrelated modules.
- Preserve configuration contracts used by host/runtime code.
- If MCP behavior changes, update the module docs and targeted tests together.
- Do not advertise or silently accept deprecated MCP methods such as `sampling/createMessage`, `roots/list`, `notifications/roots/list_changed`, `logging/setLevel`, or `notifications/message`.

## Minimal Validation
- `python -m compileall src/mash/mcp`
- Verify one manager/config path and one client or server path.

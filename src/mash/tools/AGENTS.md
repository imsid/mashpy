# AGENTS Guide for `src/mash/tools`

## What Must Stay True
- Tool definitions exposed to the model keep the expected schema contract.
- `ToolRegistry` remains the registration surface for agent tools.
- Built-in tools such as bash, MCP, and subagent invocation stay isolated in this package.

## Change Rules
- Keep tool execution behavior reusable across primary agents and subagents.
- Preserve the `InvokeSubagent` contract used by runtime host composition.
- If tool schemas or runtime metadata change, update dependent tests and docs together.

## Minimal Validation
- `python -m compileall src/mash/tools`
- Verify one tool registration path and one subagent tool path.

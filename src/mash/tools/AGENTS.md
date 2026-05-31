# AGENTS Guide for `src/mash/tools`

## What Must Stay True
- Tool definitions exposed to the model keep the expected schema contract.
- `ToolRegistry` remains the registration surface for agent tools.
- Built-in tools such as bash, MCP, and subagent invocation stay isolated in this package.
- `requires_approval` is an attribute on the `Tool` protocol (defaults to `False`).
- Tools with `requires_approval = True` trigger a durable approval interaction before execution in the hosted runtime.
- `AskUserTool` is intercepted at the workflow level (not executed directly) and triggers a durable info/choice interaction.

## Change Rules
- Keep tool execution behavior reusable across primary agents and subagents.
- Preserve the `InvokeSubagent` contract used by runtime host composition.
- If tool schemas or runtime metadata change, update dependent tests and docs together.
- If `requires_approval` semantics change, update the runtime workflow loop and H2A RFC together.

## Minimal Validation
- `python -m compileall src/mash/tools`
- Verify one tool registration path and one subagent tool path.
- Verify that `ToolRegistry.tools_requiring_approval()` returns the expected tool names.

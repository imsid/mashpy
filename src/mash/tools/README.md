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
- `web_search.py`: web search providers (`web_search`/`web_fetch`), Parallel AI by default.
- `subagent.py`: `InvokeSubagent`, used by primary agents to call registered subagents.
- `ask_user.py`: `AskUser`, lets agents request information from the user mid-execution.
- `runtime.py`: runtime-facing helpers for tool execution.

## Tool Approval

Tools can declare `requires_approval = True` to gate execution behind user consent. When the runtime's workflow loop detects that a planned tool call targets a tool with this flag, it automatically emits a `request.interaction.create` event (type `approval`) and durably blocks until the host responds.

```python
class DeployTool:
    name = "deploy_service"
    requires_approval = True
    description = "Deploy a service to the target environment."
    parameters = { ... }

    async def execute(self, args):
        ...
```

If the user responds `"approve"`, the tool executes normally. If `"deny"` or `"skip"`, the tool call is skipped and the agent receives an error result indicating denial.

The approval check is automatic — no additional wiring is needed beyond setting the attribute. The registry exposes `tools_requiring_approval(names)` for the runtime to query which tools in a planned step need consent.

## AskUser

`AskUserTool` lets the agent ask the user a question and durably wait for their response. The runtime intercepts this tool at the workflow level and translates it into a durable interaction.

```python
from mash.tools.ask_user import AskUserTool

tools = ToolRegistry()
tools.register(AskUserTool())
```

### How It Works

AskUser is **not executed directly** by the agent's `execute()` method. Instead:

1. The agent calls `AskUser(question="...", options=[...])` as a normal tool invocation.
2. The workflow engine detects the AskUser tool call and **intercepts it before execution**.
3. The runtime creates a durable interaction (either `info` or `choice` type) and waits for the user's response.
4. The user's response is returned to the agent as a normal `ToolResult`.
5. The interaction state is persisted, so it survives runtime restarts.

This interception mechanism is essential because it allows AskUser to be durable—the agent can be paused, the runtime can restart, and the interaction will resume from where it left off.

### Interaction Types

The interaction type is determined automatically based on the `options` parameter:

- **No options** → `info` interaction: user provides free-form text response
- **With options** → `choice` interaction: user selects one or more options from the provided list

Examples:

```python
# Free-form text response (info interaction)
AskUser(question="Which database should we use?")

# Multiple choice (choice interaction)
AskUser(
    question="Which services should be deployed?",
    options=["auth", "billing", "notifications"]
)
```

### Tool Result Structure

When the user responds, the agent receives a `ToolResult` containing:

- **`content`**: the user's response (free-form text for `info`, selected option(s) for `choice`)
- **`metadata`**: includes:
  - `interaction_id`: unique identifier for tracking and debugging
  - `timed_out`: boolean flag indicating whether the interaction timed out

### Timeout Behavior

AskUser interactions have a default timeout of **1 hour** (3600 seconds). If the user does not respond within this window:

- The interaction completes with `timed_out = true`
- The `content` field will be empty or null
- The agent receives the timeout status in the tool result metadata

Agents should check the `timed_out` flag and handle gracefully:

```python
# Pseudocode: how an agent might handle a timeout
result = ask_user(question="Proceed?", options=["yes", "no"])
if result.metadata.get("timed_out"):
    # Use a default, ask again, or fail gracefully
    proceed = False
else:
    proceed = result.content == "yes"
```

### Availability and Constraints

AskUser **only works in a hosted runtime** with DBOS interaction support. In local or non-hosted contexts:

- Calling AskUser returns an error result: `"AskUser requires a hosted runtime with interaction support."`
- This is intentional—AskUser requires durable interaction infrastructure that is only available in the hosted runtime.

If you encounter this error in a hosted context, it indicates a bug in the workflow engine's tool-call interception logic.

### Agent Guidance

When using AskUser, agents should:

1. Ask clear, specific questions.
2. Provide meaningful options when appropriate (rather than always using free-form).
3. Check the `timed_out` flag in the result metadata.
4. Handle timeouts gracefully (e.g., use a sensible default, retry, or fail with a clear message).
5. Use the `interaction_id` in logs or error messages for debugging.

## Web Search

`web_search.py` defines a `WebSearchProvider` contract. A provider resolves to a single `MCPServerConfig`; the runtime feeds that through the same remote-tools path as any MCP server, so the agent ends up with plain `web_search` and `web_fetch` tools.

To enable web search you must explicitly specify a provider by returning one from `build_web_search()` on their spec; it returns `None` by default, so web search is off. There's no default, so you always know who is handling your search data. Mash ships one `WebSearchProvider`, `ParallelSearchProvider`, which offers `web_search` and `web_fetch` and requires an API key.

To add another backend (Exa, Tavily, Brave), subclass `WebSearchProvider` and return its endpoint. Nothing else in the spec surface changes.

## Role In The System
- `core` and `runtime` rely on this package for the model-visible tool surface.
- Tool schemas should stay stable and predictable for prompts, execution, and tests.

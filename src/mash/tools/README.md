# Tools

`src/mash/tools` contains the built-in tools exposed to Mash agents.

## What This Package Does
- Defines the base tool contract and registration surface.
- Houses built-in tools such as the sandbox tools, MCP-backed tools, and subagent invocation.
- Provides runtime-facing helpers so tool execution can stay consistent across hosted agents.

## Main Components
- `base.py`: tool interface and shared behavior. A `ToolResult` may carry
  `images` (`ToolImage`: media type and base64 data), such as a screenshot;
  the runtime passes them to the model with the text, and each provider
  adapter maps them to its API. A request's context keeps the three most
  recent images; older ones become a note.
- `registry.py`: registration and lookup of enabled tools.
- `sandbox.py`: the sandbox tools. They run nothing locally: each call goes to
  the host's `/sandbox` relay with the agent's token and the request id, and
  the host acts in the sandbox of the request's user, in
  `/home/agent/workspaces/<agent_id>` (see `mash.sandbox`).
  - `run_code(language, code, timeout=60)`: `python`, `javascript` or `shell`.
    The code goes as stdin to a command that writes it to
    `.mash/runs/<uuid>.<ext>` as the `agent` user, runs it with `python3`,
    `node` or `bash`, and deletes it. Each call is a new process; files persist.
    The description lists what Mash's sandbox image holds and how results reach
    the user (`$MASH_FILES_URL`, `$MASH_PORTS_URL`); keep it in step with
    `Dockerfile.sandbox`. The spec's `sandbox_env()` variables
    (`SandboxVariable`) go with each call as `env`; the description names them
    with their descriptions, never their values, and `check_sandbox_env`
    refuses names the relay would.
  - `edit_file(path, old_text, new_text)`: reads the file through
    `/sandbox/files`, replaces `old_text`, which must appear exactly once, with
    `new_text`, and writes it back; with `old_text` empty it creates a file
    that does not exist yet. A missing match, more than one match, or a file
    that is not UTF-8 fails with a reason and leaves the file as it was.
  - `sandbox_put(src, dest)` / `sandbox_get(src, dest)`: copy one file (up to
    100 MB) between the agent process's machine and the folder through
    `/sandbox/files`. They exist only with file roots (`FileRoots`, from the
    spec's `sandbox_file_roots()`): the path on the agent's machine must
    resolve, symlinks followed, to a regular file inside a root, and `/proc`,
    `/sys` and `/dev` are refused even inside one. Reads and writes open the
    file with `O_NOFOLLOW` and check it with `fstat`. The descriptions name the
    roots.
  The runtime registers `run_code` for every agent when the host has
  sandboxes, and the file tools when the spec also declares roots, unless the
  spec's `enable_sandbox()` returns False. A spec that registers a tool with
  one of the three names then fails to start.
- `browser.py`: the browser tools, for an agent whose spec lists sites
  (`browser_sites()`) on a host with browsers. They run nothing locally: each
  call goes to the host's `/browser` relay with the agent's token and the
  request id, and the host acts in the browser of the request's user, on the
  spec's sites (see `mash.browser`).
  - A subset of Playwright MCP's tools under their own names, with the schemas
    of the pinned version (`mash.browser.catalog`): navigate and go back, read
    the page as an accessibility tree with refs (`browser_snapshot`,
    `browser_find`), act by ref (click, hover, type, fill a form, select,
    press a key), dialogs, waits, tabs, screenshots, and clicks and scrolls by
    coordinates. Page JavaScript and raw file uploads are not relayed.
    Results carry the page between `<web-content>` tags, and every
    description tells the model that page text is data from the site.
  - `browser_save(download_id, path)`: copies a download into the agent's
    sandbox folder. `browser_upload(target, element, path)`: gives a file from
    that folder to the file chooser `target` opens.
  - `browser_handoff(reason)`: hands the browser to the user, for a sign-in, a
    security code or a captcha. The durable runtime intercepts it as a
    `browser` interaction with a live-view link; its `execute` only runs
    outside one, and fails. In a workflow request it fails at once.
  A spec that registers a tool with one of these names fails to start on a
  host with browsers.
- `mcp.py`: MCP-backed tool integration.
- `web_search.py`: web search providers (`web_search`/`web_fetch`), Parallel AI by default.
- `subagent.py`: `InvokeSubagent`, used by primary agents to call registered subagents.
- `ask_user.py`: `AskUser`, lets agents request information from the user mid-execution.
- `runtime.py`: the runtime tools `search_conversations` and
  `get_full_turn_message`. They reach the current session or the request
  user's sessions with the agent, and never another user's turns.

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

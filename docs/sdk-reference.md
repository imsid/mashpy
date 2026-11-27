# Mash — Building Agents and Workflows

Mash is a self-hosted, durable runtime for code-authored automations: agents
and workflows deployed behind one API, on your own Postgres. A workflow is an
ordered pipeline of typed steps, a step is deterministic Python or one run of
a harnessed agent, and control flow stays in code. This file gives coding
agents (Claude Code, Codex, Cursor, etc.) everything they need to scaffold and
build a Mash-powered automation from a user prompt.

**Install:**

```bash
# install the library
uv add mashpy

# install the `mash` CLI on your PATH
uv tool install mashpy 
```

**Configure the environment:**

Requires Python >= 3.10. The host runs every agent as its own subprocess. With
a Docker daemon it can reach, it also starts a sandbox container per policy
where agents run code; without one, agents get no sandbox tools.
Set environment variables for the LLM provider you plan to use:
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`, a Postgres URL for
the durable runtime in `MASH_DATABASE_URL`, and a host secret in
`MASH_HOST_SECRET` (`openssl rand -hex 32`), from which the host derives each
agent's and each sandbox's token.

## Core Concepts

- **AgentSpec** — abstract contract that defines one agent (id, tools, skills,
  LLM provider, config). You subclass this.
- **AgentMetadata** — self-description supplied when an agent is registered
  (display name, description, capabilities, usage guidance). The companion
  routes host requests by it.
- **HostBuilder** — fluent builder that composes a flat pool of agents,
  workflows, and optional host definitions into a `Pool`.
- **Pool** — the deployed pool of agents and workflows that the API
  server runs. The pool is the unit of deploy; a pool with no user agents is a
  valid, workflow-only deploy. The pool runs no agents itself: when the host
  starts, it runs each agent as a subprocess and keeps an HTTP client for it.
- **Agent process** — a subprocess of the host (`mash agent serve`) that serves
  one agent's runtime behind an H2A HTTP server on a localhost port. It is the
  app's own code, so it inherits the host's environment, except
  `MASH_HOST_SECRET` and `MASH_API_KEY`. The host restarts one
  that exits, and agents can be enabled and disabled on a running host.
- **App** — the application posting events, for example one Slack bot, named
  by the `app_id` every event carries. It scopes memory: Cortex is built per
  app, and conversation search reaches the session or the whole app.
- **Policy** — a named, durable resource that many sessions share, created
  with `PUT /api/v1/policies/{policy_id}`. It owns the sandbox and the browser
  profile and sets the rules for requests under it: whether agents get the
  sandbox, which browser sites they may open, and how long an interaction may
  wait.
- **Session and event** — every input to a host is an event, posted to
  `POST /api/v1/sessions/{session_id}/events`. A session is one conversation,
  for example a Slack thread; its first event binds it to a host and a
  policy. Events in a session run one at a time, in arrival order, each as one
  request on the host's companion; the rest wait in the session's queue.
- **Sandbox** — a container per policy, started on first use, where the code an
  agent's model writes runs. It runs Mash's sandbox image
  (Python, Node, SQLite, git) and holds that policy's workspaces, one folder per
  agent at `/home/agent/workspaces/<agent_id>`, and nothing else: no secrets,
  no database access, unless a spec passes its own code a variable with
  `sandbox_env()`. Every agent gets `run_code` and `edit_file` when the host has sandboxes,
  so it can write and run code, change files, keep files and databases, and hand the user
  links to what it made, without registering anything; a spec opts out with
  `enable_sandbox()`, and a policy with `"sandbox": false`. An agent gets
  `sandbox_put` and `sandbox_get`, which copy
  files between its machine and the sandbox, only when its spec declares
  `sandbox_file_roots()`. Each agent's code runs as a uid of its own, with a
  clean environment, in a folder only that uid can use.
- **Browser** — a Chromium per policy, in its own container next to the
  policy's sandbox, whose profile keeps the sites the user signed in to. An
  agent whose spec lists sites with `browser_sites()` gets the browser tools,
  which drive it through the host on those sites, narrowed by the policy's
  `browser_sites`. When a site needs a sign-in, the
  agent hands the browser to the user through a live-view link; Mash never
  sees the password. Code in the sandbox has no route to the browser.
- **Host** — an immutable composition over the pool (`host_id`, `agents`,
  `workflows`). Every request to a host goes to the built-in `companion`,
  which routes it to the host's agents and workflows. The unit of composition: define hosts in code at
  build time or dynamically over the API, and send events to one by naming
  its `host_id`. Hosts are in-memory; redefine them after a restart. A host
  with one agent is how an app talks to that agent.
- **ToolRegistry / Tool** — register callable tools the agent can use.
  Tools implement `name`, `description`, `parameters` (JSON schema),
  `requires_approval`, `async execute(args) -> ToolResult`, and
  `to_llm_format()`.
- **SkillRegistry / Skill** — optional markdown instruction bundles loaded
  on demand via a meta-`Skill` tool.
- **LLMProvider** — abstract LLM contract. Shipped adapters:
  `AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`, and
  `OSSCompatibleProvider` (open-source models over any Chat Completions
  endpoint, with `GemmaProvider`/`QwenProvider`/`DeepSeekProvider`/`LlamaProvider`
  presets).
- **WorkflowSpec / CodeStep / AgentStep** — durable, observable ordered step
  pipelines. Code owns the control flow; nondeterminism is contained inside
  typed steps with schema-checked boundaries.
- **Cortex** — opt-in per-app memory managed by the host and shared by every
  agent in the pool. Requests see their app's profile and can call
  `search_cortex`; the on-demand `cortex-build` workflow builds it
  from the app's conversations, and apps inspect and edit it over the API.

The authoring surface imports from the package root:

```python
from mash import (
    AgentSpec, AgentMetadata, Host, HostBuilder, Pool,
    WorkflowSpec, CodeStep, AgentStep, StepContext,
)
```

## Minimal Agent Scaffold

This is the starting template. Every Mash app follows this structure:

```python
# my_agent/spec.py
from mash import AgentMetadata, AgentSpec, Host, HostBuilder
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.skills import SkillRegistry
from mash.tools.registry import ToolRegistry


class AssistantAgent(AgentSpec):
    def get_agent_id(self) -> str:
        return "assistant"

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(agent_id="assistant")

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            agent_id="assistant",
            system_prompt="You are a helpful assistant.",
        )


def build_pool():
    return (
        HostBuilder()
        .agent(
            AssistantAgent(),
            metadata=AgentMetadata(
                display_name="Assistant",
                description="General-purpose assistant.",
                capabilities=["conversation"],
                usage_guidance="Default agent for user requests.",
            ),
        )
        .host(Host(host_id="assistant", agents=("assistant",)))
        .build()
    )
```

Run it. The host starts one subprocess per agent. For agents to get the
sandbox tools, it also needs a Docker daemon; sandboxes run
`ghcr.io/imsid/mash-sandbox:<mashpy version>`, which the host pulls on first
start (`MASH_SANDBOX_IMAGE` names another):

```bash
export MASH_HOST_SECRET=$(openssl rand -hex 32)
mash host serve --host-app my_agent.spec:build_pool --port 8000
```

Create a policy, connect and talk to it. Every event names an app and a
policy, so the connection saves both:

```bash
echo '{"sandbox": true}' | mash policy put dev --file -
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret \
  --app-id my-app --policy-id dev
mash repl          # the deployment's only host; --host <id> picks one
```

For a script, `mash run` posts one event to a new session, prints its answer
on stdout and exits:

```bash
mash run "summarize yesterday's errors"
```

## App Clients

Ship a branded terminal client with `from mash.cli import App`. Construct
`App(name="fudge", default_host="fudge", default_base_url="http://localhost:8001",
env_prefix="FUDGE", default_app="fudge", default_policy="fudge")` and
return `app.main()` from the project's console entrypoint. `default_app` and
`default_policy` take a string or a function that returns one. It opens the
REPL with no command and also provides `run`, `workflow`, `sessions` and
`status`. `commands`, `theme`, `tool_lines` and `workflow_renderers`
customize it; operator commands are off by default. See the CLI README for
connection precedence and examples. The entrypoint is `mash.cli.app:main`;
`python -m mash.cli` runs it as a module.

## Minimal Workflow-Only Scaffold

A pool needs no user agents. A workflow of pure `CodeStep`s deploys the same
way and serves only workflow runs:

```python
# my_workflow/spec.py
from mash import CodeStep, HostBuilder, StepContext, WorkflowSpec
from pydantic import BaseModel


class ExportIn(BaseModel):
    table: str

class ExportOut(BaseModel):
    rows: int


def export(inp: ExportIn, ctx: StepContext) -> ExportOut:
    ...


def build_pool():
    return (
        HostBuilder()
        .workflow(
            WorkflowSpec(
                workflow_id="export",
                input_model=ExportIn,
                steps=[CodeStep(step_id="export", run=export, input=ExportIn, output=ExportOut)],
            )
        )
        .build()
    )
```

Run it the same way, then start runs over the API
(`POST /api/v1/workflow/export/run`) or the CLI
(`mash workflow run export --input '{"table": "bills"}'`). `mash workflows` lists the
deployment's workflows.

## AgentSpec Contract

Every agent must implement these abstract methods:

| Method | Returns | Purpose |
|---|---|---|
| `get_agent_id()` | `str` | Stable identifier for storage, routing, logs |
| `build_tools()` | `ToolRegistry` | Tools the agent can call |
| `build_skills()` | `SkillRegistry` | Optional instruction bundles |
| `build_llm()` | `LLMProvider` | Which model to use |
| `build_agent_config()` | `AgentConfig` | System prompt and behavior settings |

Optional overrides:

| Method | Default | Purpose |
|---|---|---|
| `build_memory_store()` | Postgres (requires `MASH_DATABASE_URL`) | Custom memory backend |
| `build_mcp_servers()` | `[]` | MCP server connections |
| `enable_runtime_tools()` | `True` | Auto-register runtime tools |
| `enable_sandbox()` | `True` | Get the sandbox tools on a host with sandboxes, unless the request's policy turns them off |
| `sandbox_file_roots()` | `[]` | Folders on the agent's machine that `sandbox_put`/`sandbox_get` may use; without any, the agent gets neither |
| `sandbox_env()` | `{}` | `MASH_*` variables, as `SandboxVariable(value, description)`, that this agent's code sees in the sandbox |
| `browser_sites()` | `[]` | Origins the agent may open in the policy's browser, e.g. `https://*.example.com`; without any, the agent gets no browser tools |
| `build_web_search()` | `None` | Web search provider (`web_search`/`web_fetch`); off until set |
| `on_startup(runtime)` | No-op | Hook after runtime init, in the agent's process |
| `on_shutdown(runtime)` | No-op | Hook before cleanup, in the agent's process |

The host imports your specs too (for metadata, workflows and eval snapshots),
but runtimes, tools, MCP connections and these hooks exist only in the agent's
process.

The agent process calls `build_tools()`, `build_skills()` and `build_llm()`
once, when it starts. Those objects live for the process and serve every
request at once, so a tool or LLM provider keeps no per-request state on
`self`: it reads the request's context with
`mash.logging.get_request_metadata()`, `get_session_id()`, `get_trace_id()`
and the other `mash.logging` helpers. `get_session_id()` is the request's own
session; inside a `RunSubAgents` child that is the child's session.
`get_event_session_id()` is the session the user's event was posted to (the
one `/resume` and `POST /sessions/{id}/events` use), at any subagent depth,
and `None` in a request a workflow started. The runtime calls a tool's `shutdown()`, `close()` or
`aclose()` once, when the process shuts down.

## AgentConfig Fields

```python
AgentConfig(
    agent_id="my-agent",                  # required, must match get_agent_id()
    system_prompt="You are ...",          # required, str or list of content blocks
    max_steps=30,                         # max think/act loops per request
    max_tokens=4096,                      # LLM output token cap per response
    temperature=1.0,                      # sampling temperature
    skills_enabled=True,                  # register the Skill meta-tool when skills exist
    prompt_caching_enabled=True,          # provider prompt caching
    streaming_enabled=True,               # stream tokens + emit chat.delta events
    conversation_history_turns=3,         # earlier turns replayed as text (0 = none)
    max_parallel_tools=8,                  # cap on parallel-safe tool calls run concurrently per turn
    subagent_timeout_seconds=3600,        # RunSubAgents wait: the default and the most a call may ask for
    compaction_token_threshold=500_000,   # summarize once the last turn's context reaches this (0 = off)
    tool_result_context_chars=20_000,     # longer tool results are saved to a sandbox file the model reads
    compaction_temperature=0.0,           # sampling temperature for the summary pass
    provider_options={},                  # sent with every model call, provider-specific (e.g. {"thinking_level": "low"} on Gemini)
    extra={},                             # free-form dict for provider/app-specific options
)
```

A request replays the text of its session's last `conversation_history_turns`
turns: each user message and reply, with the agents and workflows that handled
it before the next message. Tool calls and results are not replayed. Each turn
records `context_tokens`, the context its last model call read and wrote
(all input, cached included, plus output, as the provider reports it);
once the latest turn's reaches `compaction_token_threshold`, every turn since
the last summary is summarized before the next request, and replay starts
from that summary. Lower the threshold for a model with a small context
window.

A tool result longer than `tool_result_context_chars` is saved whole to
`tool-results/<trace_id>/<tool_call_id>.txt` in the agent's sandbox folder.
The model sees a note with its size, line count, exit code and path instead of
its text, and reads the file with `run_code` (`tail`, `grep`, `sed -n`). The
event log keeps the full result. An agent without a sandbox on the request
gets the whole result in its context.

## LLM Providers

```python
from mash.core.llm import AnthropicProvider, OpenAIProvider, GeminiProvider

# Anthropic (default model from ANTHROPIC_MODEL env, fallback claude-haiku-4-5-20251001)
llm = AnthropicProvider(agent_id="my-agent")
llm = AnthropicProvider(agent_id="my-agent", model="claude-sonnet-4-6-20250514")

# OpenAI (default from OPENAI_MODEL env, fallback gpt-5-mini)
llm = OpenAIProvider(agent_id="my-agent")
llm = OpenAIProvider(agent_id="my-agent", model="gpt-5")

# Gemini (default from GEMINI_MODEL env, fallback gemini-3.5-flash)
llm = GeminiProvider(agent_id="my-agent")
llm = GeminiProvider(agent_id="my-agent", model="gemini-2.5-pro")
```

### Open-source models

`OSSCompatibleProvider` runs open-source models through the same harness over any
OpenAI Chat Completions endpoint — self-hosted vLLM/Ollama/llama.cpp or a hosted
gateway (Together, Groq, OpenRouter). Mash is the client; you run or pay for the
endpoint. Swapping the provider in `build_llm()` is the only change.

The model must be served with native tool calling so the runtime can pass
`tools=` and read back `message.tool_calls`; the latest Gemma, Qwen, and DeepSeek
releases qualify. On a hosted gateway, pick a model whose route supports tool use.

```python
from mash.core.llm import (
    OSSCompatibleProvider, GemmaProvider, QwenProvider, DeepSeekProvider, LlamaProvider,
)

# Family presets (model from GEMMA_MODEL/QWEN_MODEL/DEEPSEEK_MODEL/LLAMA_MODEL env).
# base_url from OSS_BASE_URL env, fallback http://localhost:11434/v1 (Ollama).
llm = GemmaProvider(agent_id="my-agent", base_url="http://localhost:11434/v1")
llm = QwenProvider(agent_id="my-agent", model="Qwen/Qwen3-32B",
                   base_url="http://gpu-box:8000/v1")  # vLLM

# Generic endpoint (e.g. a hosted gateway with a key).
import os
llm = OSSCompatibleProvider(
    agent_id="my-agent",
    model="deepseek-ai/DeepSeek-V3",
    base_url="https://api.together.xyz/v1",
    api_key=os.environ["TOGETHER_API_KEY"],
)
```

## Custom Tools

```python
from mash.tools.base import ToolResult

class MyTool:
    name = "my_tool"
    description = "Does something useful."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
        },
        "required": ["query"],
    }

    async def execute(self, args):
        result = do_something(args["query"])
        return ToolResult.success(result)

    def to_llm_format(self):
        # The registry calls this to describe the tool to the model.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

# Register it:
tools = ToolRegistry()
tools.register(MyTool())
```

For tools that need user consent before executing:

```python
class DangerousTool:
    name = "deploy"
    requires_approval = True  # runtime auto-pauses for user consent
    ...
```

Tool calls a model emits together in one turn run concurrently by default, on
both the in-process loop and the durable runtime. A tool opts out by setting
`parallel_safe = False` (the default is `True`), which makes it run alone as a
barrier — nothing in the turn runs concurrently with it. Approval-gated tools,
`AskUser`, and `RunSubAgents` are always serialized. Calls run in order, with
each maximal run of consecutive parallel-safe calls dispatched together (capped
by `max_parallel_tools`); on the durable runtime that run executes as one atomic
step. A failing tool is captured as an error result and never aborts the other
calls in the batch.

```python
class WriteLedger:
    name = "write_ledger"
    parallel_safe = False  # has ordering-sensitive side effects
    ...
```

### Built-in Tools

```python
from mash.tools.ask_user import AskUserTool

tools = ToolRegistry()
tools.register(AskUserTool())  # only works in hosted runtime
```

### Sandbox tools

The sandbox tools come from the host, not from your spec. When the host has
sandboxes, every agent gets `run_code` and `edit_file`, and an agent whose spec
declares file roots also gets `sandbox_put` and `sandbox_get`, unless the
request's policy says `"sandbox": false`. They act in the sandbox of the
request's policy, in `/home/agent/workspaces/<agent_id>`:

- `run_code(language, code, timeout=60)` runs `python`, `javascript` or `shell`
  code there as a new process, for at most 1800 seconds. Files persist between calls and across requests
  under that policy; variables do not. `pip install --user` and `npm install`
  persist in the folder. Its description tells the model what is installed and
  how to hand results to the user.
- `edit_file(path, old_text, new_text)` replaces `old_text`, which must appear
  in the file exactly once, with `new_text`, or with `old_text` empty creates
  a new text file. Otherwise it fails and says why, and the file is unchanged.
- `sandbox_put(src, dest)` copies a file from the machine the agent process
  runs on into the folder; `sandbox_get(src, dest)` copies one back. Both
  exist only for a spec that returns folders from `sandbox_file_roots()`, and
  they read and write only regular files inside those folders (symlinks are
  followed before the check; `/proc`, `/sys` and `/dev` are always refused).
  For data a tool fetched, have the tool write a file into a root and return
  its path, so the model can copy it in. Keep credentials out of every root:
  code in the sandbox can reach the internet.

```python
class AnalystAgent(AgentSpec):
    def sandbox_file_roots(self):
        return [self.get_agent_data_dir() / "exports"]
```

Without file roots, the model moves data into the sandbox by writing it into
its code, which suits tool results of a few hundred rows.

The model shows the user what its code made through links the host serves: a
file at `<path>` in the folder opens at `$MASH_FILES_URL/<path>`, and a server
the code started on port `n` at `$MASH_PORTS_URL/<n>/`. Every command sees both
variables, so the code prints the real link. Links take no API key: the token
in them, derived from `MASH_HOST_SECRET`, names the policy. `MASH_PUBLIC_URL`
sets the address links use (default: the bind address).

The sandbox holds no secrets. When an agent's code needs one, such as a
read-only database URL, the spec passes it with `sandbox_env()`. `run_code`
sends the variables with each call, so only that agent's commands see them,
and its description gives the model each name and description, never the
value. Names must match `MASH_[A-Z0-9_]+`, and `MASH_FILES_URL` and
`MASH_PORTS_URL` stay the host's; anything else fails at startup. Code in the
sandbox can reach the internet, so pass a narrowly scoped, read-only
credential.

```python
from mash.tools.sandbox import SandboxVariable

class AnalystAgent(AgentSpec):
    def sandbox_env(self):
        return {
            "MASH_SALES_DB_URL": SandboxVariable(
                os.environ["SALES_READER_URL"],
                "Read-only Postgres URL of the sales database.",
            )
        }
```

The tools reach the host's `/sandbox` relay with the agent's token and the
request id, and the host picks the request's policy. A spec that registers its own tool
with one of these names fails at startup on a host with sandboxes.

### Browser tools

The browser tools also come from the host. On a host with browsers (a Docker
daemon, and some agent listing sites), an agent whose spec returns origins
from `browser_sites()` gets them. They act in the browser of the request's
policy, in a tab of the request's own. A policy's `browser_sites` narrows the
sites to those both lists allow, and `[]` takes the tools away:

- Playwright MCP's page tools under their own names: `browser_navigate`,
  `browser_navigate_back`, `browser_snapshot` (the page as an accessibility
  tree with a ref on each element), `browser_find`, `browser_click`,
  `browser_hover`, `browser_type`, `browser_fill_form`,
  `browser_select_option`, `browser_press_key`, `browser_handle_dialog`,
  `browser_wait_for`, `browser_tabs`, `browser_take_screenshot` (an image the
  model sees), `browser_mouse_click_xy` and `browser_mouse_wheel`. Page text
  comes back between `<web-content>` tags, as data from the site.
- `browser_save(download_id, path)` copies a download into the agent's sandbox
  folder, for `run_code`; `browser_upload(target, element, path)` gives a file
  from that folder to a file input.
- `browser_handoff(reason)` pauses the request on a `browser` interaction.
  The interaction carries no link. When the user is ready to sign in, the app
  calls `POST /api/v1/sessions/{session_id}/events/{event_id}/interactions/{interaction_id}/handoff`,
  which mints a live-view link valid for `MASH_BROWSER_HANDOFF_SECONDS`
  (default 900) and returns `{url, expires_at}`; each call mints a new one.
  The user signs in there and the app answers `done` (the REPL does this:
  it opens the link and waits for Enter). The profile keeps the session for
  later requests. When the interaction closes, every link minted for it stops
  working. The handoff waits as long as the policy's `wait_max_seconds`. In
  a workflow run it fails at once.

```python
class BillingAgent(AgentSpec):
    def browser_sites(self):
        return ["https://*.pge.com"]
```

A URL outside the sites fails, and a click or redirect that leaves them goes
back without showing the page. List the sign-in pages the sites redirect to as
well. The browser is shared by every agent and session under the policy, so
list only what the agent's job needs. Workflow code drives the same browser
with Playwright through `ctx.browser()` (`pip install 'mashpy[browser]'`).
Apps list and clear a policy's signed-in sites under
`/api/v1/browser/policies/{policy_id}`.

### Web Search

To enable web search you must explicitly specify a provider by returning a
`WebSearchProvider` from `build_web_search()`. It returns `None` by default, so
web search is off, and there's no default provider — you always know who is
handling your search data. Mash ships one `WebSearchProvider`,
`ParallelSearchProvider`, which offers `web_search` and `web_fetch` and requires
an API key.

```python
from mash.tools.web_search import ParallelSearchProvider

class ResearchAgent(AgentSpec):
    def build_web_search(self):
        return ParallelSearchProvider(api_key="...")  # or oauth_token="..."
```

Pass the key directly or set `PARALLEL_API_KEY` / `PARALLEL_OAUTH_TOKEN`;
constructing the provider without one raises `ValueError`. The tools register
under their plain names — `web_search` and `web_fetch` — and ride the same path
as remote MCP tools.

### FunctionTool (quick inline tools)

```python
from mash.tools.base import FunctionTool, ToolResult

async def search(args):
    return ToolResult.success(f"Found: {args['query']}")

tools.register(FunctionTool(
    name="search",
    description="Search the knowledge base.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    _executor=search,
))
```

## MCP Server Integration

```python
from mash.mcp.types import MCPServerConfig

class MyAgent(AgentSpec):
    def build_mcp_servers(self):
        return [
            MCPServerConfig(
                name="my-mcp-server",
                url="http://my-mcp-server:3000/sse",
                description="My custom MCP server",
            ),
        ]
```

MCP connections open from the agent's process, so the URL resolves where the
host runs: `localhost` is the host's machine or container. The agent connects
each server and lists its tools once, when its process starts; a server that
adds tools later shows them after the agent restarts. A server that can't be
reached then is logged as a warning, and the agent runs without its tools
until it restarts. Web search providers behave the same way.

## Multi-Agent Composition

Register all agents into a flat pool, then compose hosts over it. A host is a
set of agents and workflows; every agent in it is the same kind of member.
Every pool has the built-in `companion` agent, and every event to a host
runs on it: for each message the companion decides which of the host's
agents run, how many, and whether each continues an earlier session of that
agent or starts a new one, or runs one of the host's workflows. It waits for
them and writes the answer.

```python
from mash import AgentMetadata, Host, HostBuilder

pool = (
    HostBuilder()
    .agent(
        ResearchAgent(),
        metadata=AgentMetadata(
            display_name="Research Agent",
            description="Handles research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Use for research-heavy questions.",
        ),
    )
    .agent(CodeAgent(), metadata=AgentMetadata(...))
    .host(Host(host_id="assistant", agents=("research", "code")))
    .build()
)
```

A host never lists `companion`; it routes for every host. Each agent's
`AgentMetadata` is what the companion routes by: a host request gives the
companion an `AGENTS` directory of the host's agents in its prompt, for that
request only, with guidance to split a task only when its parts do not depend
on each other.

The companion routes with `RunSubAgents`, which only it has, and only on a
request that runs an event.
`RunSubAgents(requests=[{agent_id, prompt, session_id?, output_schema?}], timeout_seconds)`
runs each item as its own child request on one of the host's agents, all at
the same time, and returns when all of them are done. The result has one entry
per item, in order: the child's `request_id`, its `session_id` and
`trace_id`, its `status` (`completed`, `failed`, `cancelled` or
`timed_out`), and its response, structured output or error, plus
`interactions` when the child asked the user something. One child's failure
stays in its entry. A call takes at most 16 items. `timeout_seconds` defaults
to the companion's `AgentConfig.subagent_timeout_seconds` (3600), which is
also the most a call may ask for. Each child gets that budget, its clock
stopped while it waits on the user; when a child's budget runs out, the tool
cancels it. If a child's agent process dies, the tool waits for the host to
restart it and reads on from the child's last event, so the child's recovered
outcome is what its entry reports. Two items may not continue the same
session.

Each child is a request in a session of its own, started by the
companion, with `parent_request_id` set to the companion's request. Without
`session_id` the child starts a new session, named after its request id. With
the `session_id` of an earlier entry it continues that session and replays its
turns, so the agent sees its own earlier work there; the relay accepts only a
session of the same agent started from the companion's session. The
companion's saved turn keeps the routes of its children (agent and session,
or workflow and run, with trace and status) in `metadata["routes"]`; it replays
its last 10 turns, with each reply's routes before the next user message, so a
follow-up can go back to the session that answered.
After reading the results, the companion forwards one child's completed
answer unchanged with `ForwardAnswer(request_id)`, which ends the request with
that answer as the reply and no further model call, or reruns the session with
a corrected brief, or writes its own reply. The
companion's process starts children through the host's `/h2a` relay, which
checks each against the host composition stored with the companion's request
(not one the caller sends) and forwards it to the child agent's process.
Cancelling a request cancels its running children.

The app talks only to the companion's request, and the companion relays its
children's interactions there without a model call. When a child pauses on an
approval, an `AskUser` question or a `browser_handoff`, `RunSubAgents` opens
the same interaction on the companion's request, with an `origin` naming the
child (`agent_id`, `request_id`, `session_id`, `trace_id`, `interaction_id`).
Its id is derived from the child's request id and interaction id, and every
interaction id is derived from its request and its place in the loop, so a
recovered request arrives at the same ids. The app answers it like any other
interaction, on the event (see Sessions and Events), and the tool forwards
the answer to the child through `/h2a`. The child keeps
its deadline and its timeout default; when the child's interaction closes, the
companion's copy closes with the same response. The child's entry in the
result lists each relayed interaction with the response, so the companion's
model knows what the user decided.

When the host lists workflows (its own, not the pool defaults that
`enable_evals()` adds), each request gets a `WORKFLOWS` list in its context:
every workflow's id, `metadata["display_name"]`, `metadata["description"]` and
input schema. The companion runs one with
`RunWorkflow(workflow_id, input, timeout_seconds)`, which starts a run under
the request's app and policy through the host's `/h2a` relay and waits for its
result. On a timeout it returns `timed_out` with the `run_id`, and the run
keeps going. The REPL shows the step the run is on, and `/subagent` lists the
last request's children or shows one (`/subagent 2`). A child stops when its
parent's event is cancelled.

The companion reads what its agents did with three tools, each keyed by an
`agent_id` with a `session_id` and/or `trace_id` from its routes or a
`RunSubAgents` result: `ListAgentTraces` (an agent session's recent requests),
`InspectAgentTrace` (one trace's analysis by default, its messages, tool calls
and results with `view="transcript"`, or its events with `view="events"`) and
`GetAgentTurns` (what an agent was asked and answered). Each refuses a trace or
session that is not a child of the companion in the request's session.

`companion` picks its provider like masher's agents;
`COMPANION_AGENT_<PROVIDER>_MODEL` overrides the model. On Gemini it routes
with `thinking_level` `low` (about 1.5-2s faster per model call than the
default); `COMPANION_AGENT_THINKING_LEVEL` sets another level, and an empty
value leaves the model's default. Without any provider key it still starts,
and an event fails with an error that names the keys to set.

Hosts can also be defined dynamically on a running pool, in code or over the
API:

```python
pool.define_host(Host(host_id="research-only", agents=("research",)))
```

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/hosts/research-only \
  -H "Content-Type: application/json" \
  -d '{"agents": ["research"], "workflows": []}'
```

A request snapshots the host composition when its event starts, so
redefining a host never affects running requests. An event for a host that
is not defined, such as an API-defined host after a restart, stays queued
until `PUT /hosts` defines it again.

From the CLI, `mash compose` defines the host on the deployment (idempotent
PUT) and pins later commands to it:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --app-id my-app --policy-id dev
mash agents                                         # see what's in the pool
mash compose --host assistant --agents email,calendar
mash repl                                           # pinned to 'assistant'
```

The REPL's host is fixed for its lifetime; to change composition, exit and
`mash compose` again.

**Database connections:** each agent process holds its own Postgres
connections; the host holds one set for its reads (traces, telemetry, sessions,
workflow runs) and for the policies, sessions and events tables. Session reads
on the host cover agents that use the default `build_memory_store()`; an agent
that overrides it keeps its store in its own process. Sandboxes have no
database access unless a spec passes a URL with `sandbox_env()`.

**Enabling and disabling agents:** every agent `build_pool()` registers starts
enabled. `POST /api/v1/agent/{agent_id}/disable` stops its process and
`.../enable` starts it again; `GET /api/v1/agent` reports each agent's `enabled`
flag and `status` (`running`, `starting`, `unavailable`, `disabled`). Disable
returns `409 AGENT_IN_USE` while a host or workflow names the agent, and a host
cannot name a disabled agent. Requests in flight stay pending and continue
when the agent is enabled. The enabled set is in memory and resets on
restart. Adding an agent that is not in `build_pool()` takes a deploy.

## Policies

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/policies/acme-ws \
  -H "Content-Type: application/json" \
  -d '{"sandbox": true, "browser_sites": ["https://*.pge.com"], "wait_max_seconds": 86400}'
```

- `policy_id`: 1 to 255 letters, digits, `.`, `_`, `:`, `@` or `-`.
- `sandbox` (default `true`): `false` removes `run_code`, `edit_file`,
  `sandbox_put`, `sandbox_get`, `browser_save` and `browser_upload` from every
  agent for requests under the policy, and the `/sandbox` relay refuses them.
- `browser_sites` (default `null`): `null` keeps each agent's own
  `browser_sites()`. A list is intersected with each agent's sites, so it
  narrows and never widens. `[]` means no browser tools.
- `wait_max_seconds` (default 86400, at least 60, `null` for no cap): caps
  every interaction's deadline. An approval waits 300s and an `AskUser`
  question 3600s unless the cap is lower, and a browser handoff waits for the
  cap. At the deadline the agent gets the timed-out answer (an approval
  denied, the question's default, a failed handoff) and finishes its turn.

`PUT` replaces the whole policy and `GET` reads it; both are kept in
Postgres. Each request takes a snapshot of its policy when its event starts.
`DELETE /api/v1/policies/{policy_id}` removes the policy's sandbox container
and volume and its browser container and profile, then the policy. It
returns `409 POLICY_IN_USE` while a session on the policy has a queued or
running event. Sessions and their ledgers stay, and a later event naming the
policy gets `404 POLICY_NOT_FOUND`. From the CLI: `mash policy put <id> --file
policy.json`, `mash policy get <id>` and `mash policy rm <id>`.

Several sessions on one policy can run at once in one sandbox and one
browser. Each request gets its own browser tab and each agent its own sandbox
folder.

## Sessions and Events

Every input to a host is an event, posted to its session:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sessions/slack:T1:1731864100.0001/events \
  -H "Content-Type: application/json" \
  -d '{"event_id": "1731864100.0001", "host_id": "assistant", "app_id": "slack-bot",
       "policy_id": "acme-ws", "message": "download my latest PG&E bill"}'
```

- `event_id`, `host_id`, `app_id`, `policy_id` and `message` are required.
  `context` (prompt text), `metadata` and `structured_output` (a JSON schema)
  are optional.
- The first event creates the session and binds it to its host and policy. An
  event naming another host gets `409 SESSION_HOST_MISMATCH`, and one naming
  another policy `409 SESSION_POLICY_MISMATCH`. `app_id` isn't bound: each
  event carries its own. Session ids are global, so an app that can't
  guarantee unique ids prefixes them. `session_id` and `event_id` take 1 to
  255 letters, digits, `.`, `_`, `:` or `-`; `app_id` also allows `@`.
- A new event returns `202 {event_id, request_id, status, seq}`. The
  `request_id` is derived from the session and event ids, so it is known
  before the event runs. Posting an `event_id` the session already has
  returns `200` with the existing row and changes nothing, so a retried POST
  or a redelivered webhook runs once.
- Events in a session run one at a time, in arrival order, and the others
  wait `queued`. Work that should run in parallel goes to another session.

Each event runs as one request on the host's companion. Its status is
`queued`, `running`, `waiting` (the request has an open interaction), then
`completed`, `failed` or `cancelled`. When the request ends, the host copies
its `response` or `error` onto the event, which keeps them after retention
deletes the request.

| Route | What it does |
|---|---|
| `GET /api/v1/sessions/{session_id}/events/{event_id}/stream?after=` | The request's SSE stream: deltas, tool calls, interactions and the end frame, each with its `seq` as `id:` for reconnects. A queued event's stream holds with an `event.queued` frame and keepalives until it starts; one cancelled while queued gets `event.cancelled`. |
| `GET /api/v1/sessions/{session_id}/events/{event_id}` | The event's state: `request_id`, `seq`, `status`, open `interactions`, `response`, `error` and timestamps. |
| `GET /api/v1/sessions/{session_id}/events` | The session's ledger in `seq` order, filtered by `status` (comma-separated), `after_seq` and `limit`. |
| `GET /api/v1/hosts/{host_id}/events` | The same across every session on the host; `status=running,waiting` finds what an app was following before it restarted. |
| `POST /api/v1/sessions/{session_id}/events/{event_id}/cancel` | Cancels a queued event, or a running one's request and its children. `409 EVENT_TERMINAL` once it has ended. |

An app answers an interaction by posting to the same endpoint with the
event's id:

```json
{"event_id": "1731864100.0001", "interaction_id": "itr_…", "response": "approve"}
```

The body holds only those three keys. The answer returns `404
INTERACTION_NOT_FOUND` when the request has no such open interaction, `400
INVALID_INTERACTION_RESPONSE` when the response doesn't fit the interaction's
schema, and `409 INTERACTION_CLOSED` when another answer, a timeout or a
cancel closed it first. Repeating the stored answer returns `200`. A message
sent while an event waits on the user is a new event and queues behind it.

Requests start only from a session's queue; no route submits, resumes,
reruns or cancels a request directly. The agent read routes stay: a
request's status and events, sessions, history and traces.
`GET /api/v1/agent/{agent_id}/sessions/{session_id}/requests` lists a
session's requests newest first, filtered by `status` and `started_by`
(`user`, or the agent that started a `RunSubAgents` child).

`metadata` is opaque caller context for code, never shown to the model (the
inverse of `context`, which is prompt text). It is stored on the request, so
a crash-replayed request reruns with the same metadata; tools read it during
execution via `mash.logging.get_request_metadata()`, and the companion's
children inherit it.

A body under `/api/v1` may be at most 4 MiB; a larger one gets
`413 BODY_TOO_LARGE` before the API key is checked.

The host is the trust boundary. The app asserts `app_id` and `policy_id`,
and the host stores them on the request and its turns and never shows them
to the model. The companion's children carry the same app and policy, set by
the host's `/h2a` relay, so an agent cannot act under another policy.
`search_conversations` takes scope `session` (this agent's turns in this
session, the default) or `app` (this agent's turns in every session of the
app).

In the REPL each message is an event in the REPL's session. A message sent
while one runs is posted at once and shows as queued; Up on an empty input
takes the last queued one back to edit, cancelling its event. Ctrl-C cancels
the running event, and Ctrl-O shows each model step and each child's tool
calls.

## Workflows

A workflow is an ordered **step pipeline**: each step is a `CodeStep`
(deterministic Python) or an `AgentStep` (one agent-loop run). Every step has a
pydantic `input`/`output`; step *n*'s output threads into step *n+1*'s input
(merged over the immutable `workflow_input`), and the final step's output is the
run result. Runs are durable (resume from the failed step) and observable (a
per-step audit trail in the workflow store).

```python
from pydantic import BaseModel
from mash import AgentStep, CodeStep, StepContext, WorkflowSpec


class ScanIn(BaseModel):
    repo_url: str

class ScanOut(BaseModel):
    files_changed: list[str]
    head_sha: str

class SummaryOut(BaseModel):
    summary: str
    head_sha: str


def scan(inp: ScanIn, ctx: StepContext) -> ScanOut:
    ...  # deterministic; author owns idempotency via ctx.run_id/ctx.step_id


workflow = WorkflowSpec(
    workflow_id="changelog",
    input_model=ScanIn,  # types workflow_input; enables strict build-time checks
    steps=[
        CodeStep(step_id="scan", run=scan, input=ScanIn, output=ScanOut),
        AgentStep(step_id="summarize", agent_id="writer", input=ScanOut, output=SummaryOut),
    ],
)

pool = (
    HostBuilder()
    .agent(ConciergeAgent(), metadata=AgentMetadata(...))
    .workflow(workflow)
    .build()
)
```

- An `AgentStep`'s `output` may be a pydantic model or a JSON-schema dict; either
  becomes the request's structured-output schema. `input` may be `None`
  (passthrough) for agents that read `workflow_input` directly.
- Dynamic control flow (fan-out over rows, branching) lives inside a `CodeStep`
  body, deduped on stable keys from `StepContext`; there is no other execution
  shape.
- A run acts for an app under a policy: `POST /workflow/{id}/run` takes
  `app_id` and `policy_id` next to `input`, both required when the workflow
  has an `AgentStep` or a `CodeStep` that declares `agent_ids`, and
  `404 POLICY_NOT_FOUND` when the policy doesn't exist. The run stores both
  with a snapshot of the policy, `StepContext.app_id`, `.policy_id` and
  `.policy` expose them to code steps, and every agent step submits under
  them, so its code runs in that policy's sandbox. A resumed run keeps its
  app and policy snapshot. Runs have no session and take no session queue.
- Run, resume, inspect, and stream over the API (`POST /workflow/{id}/run`,
  `.../runs/{run_id}/resume`, `.../runs/{run_id}`, `.../runs/{run_id}/events`) or
  the REPL (`/workflow run|status|resume`, which runs under the connection's
  app and policy)
  or the shell (`mash workflow run|status|resume`).

## Cortex (App Memory)

Cortex is opt-in per pool. It keeps one memory per app in Postgres
(`cortex_app` and `cortex_entry`), shared by every agent in the pool and
built from every session in the app:

```python
pool = (
    HostBuilder()
    .agent(ConciergeAgent(), metadata=AgentMetadata(...))
    .enable_cortex(profile_max_chars=4000)   # default 4000
    .build()
)
```

- **Reads.** Every request gets its app's profile as an `APP MEMORY` block in
  its system prompt and a read-only `search_cortex` tool over the app's
  entries, both scoped to that app. Everything in an app can read every
  conversation in it this way; separate memory means separate apps.
- **Building.** Nothing writes Cortex after a request. Run the `cortex-build`
  workflow on demand for one app; the run's app is the one it builds:
  `POST /api/v1/workflow/cortex-build/run` with
  `{"app_id": "slack-bot", "policy_id": "acme-ws", "input": {"limit": 200}}`.
  It reads the app's turns from every agent and session
  since the last build (workflow and child turns excluded) with the tools
  each turn ran, has the built-in `cortex-agent` curate them into a profile
  and entries, and writes the result in one transaction. Rerun while the
  result's `remaining` is above 0. A build fails with a version conflict if
  the app edited the Cortex while it ran; run it again.
- **App API.** Under `/api/v1/cortex/apps/{app_id}`: `GET` (profile,
  version, entry count), `GET .../entries` (`q`, `origin`, `limit`, `after`),
  `POST .../entries` (`topic`, `content`), `PATCH`/`DELETE .../entries/{entry_id}`,
  and `DELETE` for the whole Cortex. Entries an app creates or edits are
  pinned (`origin: app`): builds never change or delete them. The profile is
  written only by builds.
- `cortex-agent` picks its provider like masher's agents;
  `CORTEX_AGENT_<PROVIDER>_MODEL` overrides the model.

## Evals

Masher's evals are opt-in per pool:

```python
pool = HostBuilder().agent(...).enable_evals().build()
```

`enable_evals()` registers `eval-agent`, `eval-judge-agent`, and the
`gen-synthetic-evals` and `run-experiment` workflows, which attach to every
host. Without it the pool has none of them.

## Structured Output

An event's `structured_output` is a JSON schema. It becomes the companion
request's structured-output schema, and the event's `response` carries the
`structured_output` the request returned. A schema that fails validation
gets `400 INVALID_STRUCTURED_OUTPUT` and no event is stored.

```json
{
  "event_id": "e-1", "host_id": "assistant", "app_id": "my-app", "policy_id": "dev",
  "message": "Analyze this data",
  "structured_output": {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "score": {"type": "number"}},
    "required": ["summary", "score"]
  }
}
```

The event's `response.structured_output` is then `{"summary": "...", "score": 0.95}`.
An `AgentStep`'s `output` sets the same schema for a workflow step.

## Feedback

The REPL ships a `/feedback` command. A user types `/feedback <message>` and the
note is stored with the host, agent, session, and last request id from the
current shell. There is no LLM step; the message is captured as written.

```bash
mash repl
› /feedback the trace output is hard to read
✓ Feedback recorded (session s-1, request r-9)
```

Feedback lands in a `runtime_feedback` table in the runtime store, alongside the
event log. App developers read it back over the API. `GET /api/v1/feedback`
takes a required `agent_id` and a required `after` unix timestamp, with optional
`before`, `session_id`, `feedback_type`, `q` (full-text over the message), and
`limit`. Submission also has its own route, `POST /api/v1/feedback`. Neither
endpoint depends on observability being enabled.

## Traces

Every request is recorded as OpenTelemetry spans: `invoke_agent <agent>` for
the run, `chat <model>` for each model call (token usage under `gen_ai.usage.*`),
`execute_tool <tool>` for each tool call, and `mash.step <n>`,
`mash.context_load` and `mash.interaction <type>` for the rest. `RunSubAgents`
children nest under the call that started them. The admin UI's trace drawer
and `GET /api/v1/telemetry/trace/analysis` show these spans, and `/trace` in
the REPL summarizes them.

To send them to Grafana, Datadog, Honeycomb or any OTLP backend, install the
extra and set an endpoint on the host:

```bash
uv add 'mashpy[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
export OTEL_SERVICE_NAME=my-agent
```

A request is exported when it ends, with its log records (`exception` on
failures) and the GenAI client metrics. Send a W3C `traceparent` header when
posting an event to put its request inside your service's trace. Prompts, completions,
tool I/O and error messages are left out unless `MASH_OTEL_CAPTURE_CONTENT=1`.

## Serving and Deployment

```bash
# Host on your machine, sandboxes in Docker
export MASH_HOST_SECRET=...
mash host serve --host-app my_agent.spec:build_pool --port 8000

# Host in a container: mount the Docker socket and add its group;
# each sandbox then runs on a network of its own, which the host joins.
# Set MASH_HOST_APP, MASH_DATABASE_URL, MASH_HOST_SECRET, MASH_API_KEY, and
# MASH_PUBLIC_URL to the address users reach the host at. The host refuses to
# bind a non-loopback address such as 0.0.0.0 without an API key.
# Without a Docker socket the host runs and its agents get no sandbox tools.

# Programmatic
from mash.api import run_host, MashHostConfig
run_host(
    pool,
    config=MashHostConfig(
        bind_host="0.0.0.0",
        bind_port=8000,
        api_key=os.environ["MASH_API_KEY"],
        host_app="my_agent.spec:build_pool",
    ),
)
```

A deployment runs one host replica, and its agent processes live and die with
it. A policy's sandbox stops after `MASH_SANDBOX_IDLE_SECONDS` (default 900)
without use and starts again on the next call or link, with the same volume;
servers the code started do not survive the stop. At
most `MASH_SANDBOX_MAX_RUNNING` (default 20) run at once. Manage them on the
Docker host:

```bash
mash sandbox ls                               # policy, status, uptime, container
mash sandbox shell <policy_id> --agent <id>   # bash as that agent's uid in its folder (or --root)
mash sandbox stop <policy_id>                 # stop it now; the volume is kept
```

Browsers run `ghcr.io/imsid/mash-browser:<mashpy version>`
(`MASH_BROWSER_IMAGE` names another), stop after `MASH_BROWSER_IDLE_SECONDS`
(default 900) and are capped at `MASH_BROWSER_MAX_RUNNING` (default 10). A
handoff link lasts `MASH_BROWSER_HANDOFF_SECONDS` (default 900) from when
it is minted and uses `MASH_PUBLIC_URL`. `mash browser ls`, `mash browser view
<policy_id>` (a live-view link) and `mash browser stop <policy_id>` manage
them. `mash policy rm` deletes both with the policy.

Each process (the host and every agent process) holds at most
`MASH_DB_POOL_MAX_SIZE` Postgres connections (default 10). Each agent process
works on at most `MASH_AGENT_MAX_RUNNING` requests at once (default 32); the
rest wait for a slot, and a request waiting on the user or on its children
holds none. Nothing is deleted
until `MASH_RETENTION_DAYS` is set; then the host deletes, every hour,
finished requests with their event logs and finished workflow runs older
than that, skipping a request whose session event is still running or whose
turn a session still replays (replay rebuilds turns from their events). The
sessions and events ledger, conversation turns, Cortex, feedback and evals
are kept.

For full deployment instructions (compose file, server deployment, the
environment reference, and external API access), see
[how-to-deploy.md](docs/posts/how-to-deploy.md).

## Project Structure Convention

```
my_agent/
  __init__.py
  spec.py          # AgentSpec subclasses + build_pool()
  tools.py         # Custom tool implementations
  prompt.py        # System prompt construction helpers
  skills/          # SKILL.md files for filesystem-backed skills
```

## Reference Documentation (fetch on demand)

These are the authoritative READMEs. Read them when you need deeper context
on a specific subsystem:

- [Package overview](https://github.com/imsid/mashpy/blob/main/src/mash/README.md)
- [Runtime & hosting](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/README.md)
- [Tools](https://github.com/imsid/mashpy/blob/main/src/mash/tools/README.md)
- [Skills](https://github.com/imsid/mashpy/blob/main/src/mash/skills/README.md)
- [LLM providers](https://github.com/imsid/mashpy/blob/main/src/mash/core/llm/README.md)
- [Workflows](https://github.com/imsid/mashpy/blob/main/src/mash/workflows/README.md)
- [Evals](https://github.com/imsid/mashpy/blob/main/src/mash/evals/README.md)
- [API server](https://github.com/imsid/mashpy/blob/main/src/mash/api/README.md)
- [CLI](https://github.com/imsid/mashpy/blob/main/src/mash/cli/README.md)
- [Memory](https://github.com/imsid/mashpy/blob/main/src/mash/memory/README.md)
- [Browser](https://github.com/imsid/mashpy/blob/main/src/mash/browser/README.md)
- [MCP integration](https://github.com/imsid/mashpy/blob/main/src/mash/mcp/README.md)
- [Cortex](https://github.com/imsid/mashpy/blob/main/src/mash/agents/cortex/README.md)

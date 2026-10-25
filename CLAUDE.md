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
a Docker daemon it can reach, it also starts a sandbox container per user where
agents run code; without one, agents get no sandbox tools.
Set environment variables for the LLM provider you plan to use:
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`, a Postgres URL for
the durable runtime in `MASH_DATABASE_URL`, and a host secret in
`MASH_HOST_SECRET` (`openssl rand -hex 32`), from which the host derives each
agent's and each sandbox's token.

## Core Concepts

- **AgentSpec** — abstract contract that defines one agent (id, tools, skills,
  LLM provider, config). You subclass this.
- **AgentMetadata** — self-description supplied when an agent is registered
  (display name, description, capabilities, usage guidance). Role-independent.
- **HostBuilder** — fluent builder that composes a flat pool of agents,
  workflows, and optional host definitions into a `Pool`.
- **Pool** — the deployed pool of role-less agents and workflows that the API
  server runs. The pool is the unit of deploy; a pool with no user agents is a
  valid, workflow-only deploy. The pool runs no agents itself: when the host
  starts, it runs each agent as a subprocess and keeps an HTTP client for it.
- **Agent process** — a subprocess of the host (`mash agent serve`) that serves
  one agent's runtime behind an H2A HTTP server on a localhost port. It is the
  app's own code, so it inherits the host's environment. The host restarts one
  that exits, and agents can be enabled and disabled on a running host.
- **User** — the end user a request acts for, named by the `user_id` every
  submit body carries. The app asserts it and the host trusts it; it scopes the
  request's conversation search, its Cortex memory, and its sandbox.
- **Sandbox** — a container per user, started on first use, where the code an
  agent's model writes runs: the user's computer. It runs Mash's sandbox image
  (Python, Node, SQLite, git) and holds that user's workspaces, one folder per
  agent at `/home/agent/workspaces/<agent_id>`, and nothing else: no secrets,
  no database access. Every agent gets `run_code`, `sandbox_put` and
  `sandbox_get` when the host has sandboxes, so it can write and run code, keep
  files and databases, and hand the user links to what it made, without
  registering anything. Code runs as an `agent` user with a clean environment.
- **Host** — an immutable composition over the pool (`host_id`, `primary`,
  `subagents`, `workflows`). The unit of composition: define hosts in code at
  build time or dynamically over the API, and route requests to one with
  `POST /v1/hosts/{host_id}/request`. Hosts are in-memory; redefine them after
  a restart.
- **ToolRegistry / Tool** — register callable tools the agent can use.
  Tools implement `name`, `description`, `parameters` (JSON schema),
  `requires_approval`, and `async execute(args) -> ToolResult`.
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
- **Cortex** — opt-in per-user memory managed by the host and shared by every
  agent in the pool. Requests see their user's profile and can call
  `search_cortex`; the on-demand `cortex-build` workflow builds it
  from the user's conversations, and apps inspect and edit it over the API.

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
from mash import AgentMetadata, AgentSpec, HostBuilder
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
        return AnthropicProvider(app_id="assistant")

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id="assistant",
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

Connect and talk to it. Every request acts for a user, so the connection
saves one:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --user-id u-1
mash repl --agent assistant
```

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
(`POST /api/v1/workflow/export/run`) or the CLI. `mash workflows` lists the
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
| `build_web_search()` | `None` | Web search provider (`web_search`/`web_fetch`); off until set |
| `on_startup(runtime)` | No-op | Hook after runtime init, in the agent's process |
| `on_shutdown(runtime)` | No-op | Hook before cleanup, in the agent's process |

The host imports your specs too (for metadata, workflows and eval snapshots),
but runtimes, tools, MCP connections and these hooks exist only in the agent's
process.

## AgentConfig Fields

```python
AgentConfig(
    app_id="my-agent",                    # required, must match get_agent_id()
    system_prompt="You are ...",          # required, str or list of content blocks
    max_steps=30,                         # max think/act loops per request
    max_tokens=4096,                      # LLM output token cap per response
    temperature=1.0,                      # sampling temperature
    skills_enabled=True,                  # register the Skill meta-tool when skills exist
    prompt_caching_enabled=True,          # provider prompt caching
    streaming_enabled=True,               # stream tokens + emit llm.response.delta events
    conversation_history_turns=3,         # prior turns replayed into context
    max_parallel_tools=8,                  # cap on parallel-safe tool calls run concurrently per turn
    compaction_token_threshold=0,         # auto-summarize history past this token count (0 = off)
    compaction_turn_limit=50,             # recent turns the summary keeps when compaction runs
    compaction_temperature=0.0,           # sampling temperature for the summary pass
    extra={},                             # free-form dict for provider/app-specific options
)
```

## LLM Providers

```python
from mash.core.llm import AnthropicProvider, OpenAIProvider, GeminiProvider

# Anthropic (default model from ANTHROPIC_MODEL env, fallback claude-haiku-4-5-20251001)
llm = AnthropicProvider(app_id="my-agent")
llm = AnthropicProvider(app_id="my-agent", model="claude-sonnet-4-6-20250514")

# OpenAI (default from OPENAI_MODEL env, fallback gpt-5-mini)
llm = OpenAIProvider(app_id="my-agent")
llm = OpenAIProvider(app_id="my-agent", model="gpt-5")

# Gemini (default from GEMINI_MODEL env, fallback gemini-3.5-flash)
llm = GeminiProvider(app_id="my-agent")
llm = GeminiProvider(app_id="my-agent", model="gemini-2.5-pro")
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
llm = GemmaProvider(app_id="my-agent", base_url="http://localhost:11434/v1")
llm = QwenProvider(app_id="my-agent", model="Qwen/Qwen3-32B",
                   base_url="http://gpu-box:8000/v1")  # vLLM

# Generic endpoint (e.g. a hosted gateway with a key).
import os
llm = OSSCompatibleProvider(
    app_id="my-agent",
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
`AskUser`, and `InvokeSubagent` are always serialized. Calls run in order, with
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
sandboxes, every agent gets three tools that act in the sandbox of the user the
request acts for, in `/home/agent/workspaces/<agent_id>`:

- `run_code(language, code, timeout=60)` runs `python`, `javascript` or `shell`
  code there as a new process. Files persist between calls and across requests
  for that user; variables do not. `pip install --user` and `npm install`
  persist in the folder. Its description tells the model what is installed and
  how to hand results to the user.
- `sandbox_put(src, dest)` copies a file from the machine the agent process
  runs on into the folder; `sandbox_get(src, dest)` copies one back. For data a
  tool fetched, have the tool write a file and return its path, so the model
  can copy it in.

The model shows the user what its code made through links the host serves: a
file at `<path>` in the folder opens at `$MASH_FILES_URL/<path>`, and a server
the code started on port `n` at `$MASH_PORTS_URL/<n>/`. Every command sees both
variables, so the code prints the real link. Links take no API key: the token
in them, derived from `MASH_HOST_SECRET`, names the user. `MASH_PUBLIC_URL`
sets the address links use (default: the bind address).

The tools reach the host's `/sandbox` relay with the agent's token and the
request id, and the host picks the user. A spec that registers its own tool
with one of these names fails at startup on a host with sandboxes.

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
host runs: `localhost` is the host's machine or container.

## Multi-Agent Composition

Register all agents into a flat pool, then compose hosts over it. A host
names one agent as primary and a set of subagents; roles live in the host,
not on the agents.

```python
from mash import AgentMetadata, Host, HostBuilder

pool = (
    HostBuilder()
    .agent(ConciergeAgent(), metadata=AgentMetadata(...))
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
    .host(
        Host(
            host_id="assistant",
            primary="concierge",
            subagents=("research", "code"),
        )
    )
    .build()
)
```

Submitting a request to a host wires the primary with an `InvokeSubagent`
tool and a prompt block describing that host's subagents — for that request
only. The same agent can be primary in one host and a subagent in another.
The primary's process calls a subagent through the host's `/h2a` relay, which
checks the call against the request's composition and forwards it to the
subagent's process.

Hosts can also be defined dynamically on a running pool, in code or over the
API:

```python
pool.define_host(Host(host_id="research-only", primary="research"))
```

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/hosts/research-only \
  -H "Content-Type: application/json" \
  -d '{"primary": "research", "subagents": [], "workflows": []}'

curl -X POST http://127.0.0.1:8000/api/v1/hosts/research-only/request \
  -H "Content-Type: application/json" \
  -d '{"message": "find recent papers", "session_id": "s-1", "user_id": "u-42"}'
```

The submit response includes the primary `agent_id` and `request_id`; stream
results from the existing `GET /v1/agent/{agent_id}/request/{request_id}/events`.
Requests snapshot the host composition at submit time, so redefining a host
never affects in-flight requests.

Submit bodies also take an optional `metadata` JSON object: opaque caller
context for code, never shown to the model (the inverse of `context`, which is
prompt text). It is stored on the request, so a crash-replayed request reruns
with the same metadata; tools read it during execution via
`mash.logging.get_request_metadata()`, and subagent requests inherit it.

Every submit body carries a `user_id`: the end user the request acts for (1 to
255 letters, digits, `.`, `_`, `:`, `@` or `-`). A body without one fails
validation with `422`. The host is the trust boundary: the app asserts the
user, and the host passes it to the primary. It is stored on the request and
its turns and never shown to the model, and the request's code runs in that
user's sandbox. Subagent calls carry the parent request's user, set by
the host's `/h2a` relay, so an agent cannot act for another user.
`search_conversations` offers scope `user`: the user's conversations with that
agent.

From the CLI, `mash compose` defines the host on the deployment (idempotent
PUT) and pins later commands to it:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --user-id u-1   # save the connection
mash agents                                         # see what's in the pool
mash compose --host assistant --primary concierge --subagents email,calendar
mash repl                                           # pinned to 'assistant'
```

The REPL target is fixed for its lifetime; to change composition, exit and
`mash compose` again (or `mash repl --agent <id>` for a bare agent).

**Database connections:** each agent process holds its own Postgres
connections; the host holds one set for its reads (traces, telemetry, sessions,
workflow runs). Session reads on the host cover agents that use the default
`build_memory_store()`; an agent that overrides it keeps its store in its own
process. Sandboxes have no database access.

**Enabling and disabling agents:** every agent `build_pool()` registers starts
enabled. `POST /api/v1/agent/{agent_id}/disable` stops its process and
`.../enable` starts it again; `GET /api/v1/agent` reports each agent's `enabled`
flag and `status` (`running`, `starting`, `unavailable`, `disabled`). Disable
returns `409 AGENT_IN_USE` while a host or workflow names the agent. A request
to a disabled agent returns `409 AGENT_DISABLED`; requests in flight stay
pending and continue when the agent is enabled. The enabled set is in memory
and resets on restart. Adding an agent that is not in `build_pool()` takes a
deploy.

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
- A run acts for a user: `POST /workflow/{id}/run` takes `user_id` next to
  `input`, required when the workflow has an `AgentStep` or a `CodeStep` that
  declares `agent_ids`. The run stores it, `StepContext.user_id` exposes it to
  code steps, and every agent step submits for it, so its code runs in that
  user's sandbox. A resumed run keeps its user.
- Run, resume, inspect, and stream over the API (`POST /workflow/{id}/run`,
  `.../runs/{run_id}/resume`, `.../runs/{run_id}`, `.../runs/{run_id}/events`) or
  the REPL (`/workflow run|status|resume`, which runs for the connection's user).

## Cortex (User Memory)

Cortex is opt-in per pool. It keeps one memory per user in Postgres
(`cortex_user` and `cortex_entry`), shared by every agent in the pool:

```python
pool = (
    HostBuilder()
    .agent(ConciergeAgent(), metadata=AgentMetadata(...))
    .enable_cortex(profile_max_chars=4000)   # default 4000
    .build()
)
```

- **Reads.** Every request (see Multi-Agent Composition) gets its user's
  profile as an `ABOUT THIS USER` block in its system prompt and a read-only
  `search_cortex` tool over the user's entries, both scoped to that user.
- **Building.** Nothing writes Cortex after a request. Run the `cortex-build`
  workflow on demand for one user; the run's user is the one it builds:
  `POST /api/v1/workflow/cortex-build/run` with
  `{"user_id": "u-42", "input": {"limit": 200}}`. It reads the user's turns
  since the last build (workflow and subagent turns excluded) with the tools
  each turn ran, has the built-in `cortex-agent` curate them into a profile
  and entries, and writes the result in one transaction. Rerun while the
  result's `remaining` is above 0. A build fails with a version conflict if
  the app edited the user's Cortex while it ran; run it again.
- **App API.** Under `/api/v1/cortex/users/{user_id}`: `GET` (profile,
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

```python
from pydantic import BaseModel

class AnalysisResult(BaseModel):
    summary: str
    score: float

response = await runtime.submit_request(
    message="Analyze this data",
    session_id="s1",
    user_id="u-1",
    structured_output=AnalysisResult,
)
# response.structured_output -> {"summary": "...", "score": 0.95}
```

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

## Serving and Deployment

```bash
# Host on your machine, sandboxes in Docker
export MASH_HOST_SECRET=...
mash host serve --host-app my_agent.spec:build_pool --port 8000

# Host in a container: mount the Docker socket and add its group;
# sandboxes then run on the `mash-sandboxes` network.
# Set MASH_HOST_APP, MASH_DATABASE_URL, MASH_HOST_SECRET, and MASH_PUBLIC_URL
# to the address users reach the host at.
# Without a Docker socket the host runs and its agents get no sandbox tools.

# Programmatic
from mash.api import run_host, MashHostConfig
run_host(
    pool,
    config=MashHostConfig(
        bind_host="0.0.0.0", bind_port=8000, host_app="my_agent.spec:build_pool"
    ),
)
```

A deployment runs one host replica, and its agent processes live and die with
it. A user's sandbox stops after `MASH_SANDBOX_IDLE_SECONDS` (default 900)
without use and starts again on the next call or link, with the same volume;
servers the code started do not survive the stop. At
most `MASH_SANDBOX_MAX_RUNNING` (default 20) run at once. Manage them on the
Docker host:

```bash
mash sandbox ls                             # user, status, uptime, container
mash sandbox shell <user_id> --agent <id>   # bash as `agent` in that agent's folder (--root for root)
mash sandbox stop <user_id>                 # stop it now; the volume is kept
```

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
- [MCP integration](https://github.com/imsid/mashpy/blob/main/src/mash/mcp/README.md)
- [Cortex](https://github.com/imsid/mashpy/blob/main/src/mash/agents/cortex/README.md)

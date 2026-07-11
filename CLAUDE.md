# Mash — Building Agents

Mash is a Python SDK and host runtime for building self-hosted multi-agent
applications. This file gives coding agents (Claude Code, Codex, Cursor, etc.)
everything they need to scaffold and build a Mash-powered agent from a user
prompt.

**Install:**

```bash
# install the library
uv add mashpy

# install the `mash` CLI on your PATH
uv tool install mashpy 
```

**Configure the environment:**

Requires Python >= 3.10. Set environment variables for the LLM provider you
plan to use: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY` 
and and a Postgres URL for its durable runtime in `MASH_DATABASE_URL`

## Core Concepts

- **AgentSpec** — abstract contract that defines one agent (id, tools, skills,
  LLM provider, config). You subclass this.
- **AgentMetadata** — self-description supplied when an agent is registered
  (display name, description, capabilities, usage guidance). Role-independent.
- **HostBuilder** — fluent builder that composes a flat pool of agents,
  optional workflows, and optional host definitions into an `AgentPool`.
- **AgentPool** — the deployed pool of role-less agents that the API server
  runs. The pool is the unit of deploy.
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
  pipelines orchestrated by DBOS.

## Minimal Agent Scaffold

This is the starting template. Every Mash app follows this structure:

```python
# my_agent/spec.py
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentMetadata, AgentSpec, HostBuilder
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry


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

Run it:

```bash
mash host serve --host-app my_agent.spec:build_pool --port 8000
```

Connect:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent assistant
```

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
| `on_startup(runtime)` | No-op | Hook after runtime init |
| `on_shutdown(runtime)` | No-op | Hook before cleanup |

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
from mash.tools.bash import BashTool
from mash.tools.ask_user import AskUserTool

tools = ToolRegistry()
tools.register(BashTool(working_dir="/path/to/workspace"))
tools.register(AskUserTool())  # only works in hosted runtime
```

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
                url="http://localhost:3000/sse",
                description="My custom MCP server",
            ),
        ]
```

## Multi-Agent Composition

Register all agents into a flat pool, then compose hosts over it. A host
names one agent as primary and a set of subagents; roles live in the host,
not on the agents.

```python
from mash.runtime import AgentMetadata, Host, HostBuilder

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
  -d '{"message": "find recent papers", "session_id": "s-1"}'
```

The submit response includes the primary `agent_id` and `request_id`; stream
results from the existing `GET /v1/agent/{agent_id}/request/{request_id}/events`.
Requests snapshot the host composition at submit time, so redefining a host
never affects in-flight requests.

From the CLI, `mash compose` defines the host on the deployment (idempotent
PUT) and pins later commands to it:

```bash
mash connect --api-base-url http://127.0.0.1:8000   # save the connection
mash agents                                         # see what's in the pool
mash compose --host assistant --primary concierge --subagents email,calendar
mash repl                                           # pinned to 'assistant'
```

The REPL target is fixed for its lifetime; to change composition, exit and
`mash compose` again (or `mash connect --agent <id>` for a bare agent).

**Connection sharing:** `AgentPool` creates one shared Postgres connection
pool and one shared memory store for all agents that use the default
`build_memory_store()`. This keeps the total database connection count
constant regardless of agent count. Agents that override
`build_memory_store()` get their own store instance.

## Workflows

A workflow is an ordered **step pipeline**: each step is a `CodeStep`
(deterministic Python) or an `AgentStep` (one agent-loop run). Every step has a
pydantic `input`/`output`; step *n*'s output threads into step *n+1*'s input
(merged over the immutable `workflow_input`), and the final step's output is the
run result. Runs are durable (resume from the failed step) and observable (a
per-step audit trail in the workflow store).

```python
from pydantic import BaseModel
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec


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
- Run, resume, inspect, and stream over the API (`POST /workflow/{id}/run`,
  `.../runs/{run_id}/resume`, `.../runs/{run_id}`, `.../runs/{run_id}/events`) or
  the REPL (`/workflow run|status|resume`).

## Structured Output

```python
from pydantic import BaseModel

class AnalysisResult(BaseModel):
    summary: str
    score: float

response = await runtime.submit_request(
    message="Analyze this data",
    session_id="s1",
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
# Local development
mash host serve --host-app my_agent.spec:build_pool --port 8000

# Docker (using the Mash base image)
# Set MASH_HOST_APP, MASH_DATA_DIR, MASH_DATABASE_URL

# Programmatic
from mash.api import run_host, MashHostConfig
run_host(pool, config=MashHostConfig(bind_host="0.0.0.0", bind_port=8000))
```

For full deployment instructions (Docker Compose, horizontal scaling,
cloud deployment, and external API access), see
[HOW_TO_DEPLOY.md](docs/HOW_TO_DEPLOY.md).

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

---
name: build-mash-agent
description: Scaffold and build a Mash-powered agent application from a user prompt.
---

# Build a Mash Agent

You are helping a developer build an agent application using the **Mash** Python
SDK (`pip install mashpy`). Mash is a framework for building self-hosted
multi-agent applications with durable execution, human-in-the-loop interactions,
and a built-in API server.

The model to keep in mind throughout:

| Concept | Role |
|---|---|
| `AgentSpec` | Contract defining one agent (id, tools, skills, LLM, config) |
| `AgentMetadata` | Required self-description registered with every agent (display name, description, capabilities, usage guidance) |
| `AgentPool` | The deployed flat pool of role-less agents — the unit of deploy |
| `Host` | A composition over the pool naming a primary and subagents — the unit of composition |
| `HostBuilder` | Fluent builder producing an `AgentPool` from agents, workflows, and host definitions |

Agents never carry roles. "Primary" and "subagent" only exist inside a `Host`,
so the same agent can be primary in one host and a subagent in another. One
pool can serve many hosts at once.

Follow the steps below to scaffold a working Mash agent from the user's
description.

## Step 1: Gather Requirements

From the user's prompt, determine:

1. **Agent purpose** — what the agent does (e.g., "customer support bot",
   "code reviewer", "data pipeline orchestrator")
2. **Tools needed** — what actions the agent can take (bash, web search,
   database queries, API calls, file operations, etc.)
3. **LLM provider** — a frontier model (Anthropic default, OpenAI, Gemini) or an
   open-source model (Gemma, Qwen, DeepSeek, Llama) served over a Chat
   Completions endpoint
4. **Multi-agent** — does the user need several specialists composed into a
   host, with a primary delegating to subagents?
5. **Workflows** — does the user need ordered task pipelines?
6. **Human-in-the-loop** — does any tool need user approval before executing?

If the user's prompt is ambiguous, make reasonable defaults and note them.

## Step 2: Scaffold the Project

Create this file structure:

```
{project_name}/
  __init__.py
  spec.py          # AgentSpec subclass(es) + build_pool()
  tools.py         # Custom tool implementations (if any)
```

### spec.py — The Agent Definition

Every Mash agent implements `AgentSpec`. Here is the minimal scaffold:

```python
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentMetadata, AgentSpec, HostBuilder
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry


class AssistantAgent(AgentSpec):
    def get_agent_id(self) -> str:
        return "assistant"

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        # Register tools here
        return tools

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

`AgentMetadata` is required for every pooled agent — registration rejects an
agent without it. It is the self-description delegation decisions are made
from, so write it like routing documentation, not marketing copy.

### tools.py — Custom Tools

Each tool is a class with `name`, `description`, `parameters` (JSON schema),
`requires_approval` (bool), and an `async execute(args) -> ToolResult` method:

```python
from mash.tools.base import ToolResult


class SearchTool:
    name = "search"
    description = "Search the knowledge base for relevant information."
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    }

    async def execute(self, args):
        query = args["query"]
        # Implement search logic here
        results = f"Results for: {query}"
        return ToolResult.success(results)
```

For quick inline tools, use `FunctionTool`:

```python
from mash.tools.base import FunctionTool, ToolResult


async def _ping(args):
    return ToolResult.success("pong")


ping_tool = FunctionTool(
    name="ping",
    description="Health check.",
    parameters={"type": "object", "properties": {}},
    _executor=_ping,
)
```

### Built-in Tools

Mash ships these tools — register them directly:

```python
from mash.tools.bash import BashTool
from mash.tools.ask_user import AskUserTool

tools = ToolRegistry()
tools.register(BashTool(working_dir="/path/to/workspace"))
tools.register(AskUserTool())  # durable user questions (hosted runtime only)
```

### Web Search

To enable web search you must explicitly specify a provider by returning one
from `build_web_search()`. It returns `None` by default, so web search is off,
and there's no default provider — you always know who is handling your search
data. Mash ships one `WebSearchProvider`, `ParallelSearchProvider`, which gives
the agent `web_search` and `web_fetch` and requires an API key:

```python
from mash.tools.web_search import ParallelSearchProvider

class ResearchAgent(AgentSpec):
    def build_web_search(self):
        return ParallelSearchProvider(api_key="...")  # or oauth_token="..."
```

Pass the key directly or set `PARALLEL_API_KEY` / `PARALLEL_OAUTH_TOKEN`;
constructing the provider without one raises `ValueError`.

### Tool Approval

Set `requires_approval = True` on any tool that should pause for user consent
before executing. The runtime handles the approval flow automatically — no
additional wiring needed.

## Step 3: Choose an LLM Provider

```python
from mash.core.llm import AnthropicProvider, OpenAIProvider, GeminiProvider

# Pick one:
llm = AnthropicProvider(app_id="my-agent")                          # Claude
llm = AnthropicProvider(app_id="my-agent", model="claude-sonnet-4-6")

llm = OpenAIProvider(app_id="my-agent")                             # GPT
llm = OpenAIProvider(app_id="my-agent", model="gpt-5")              # reasoning model

llm = GeminiProvider(app_id="my-agent")                             # Gemini
llm = GeminiProvider(app_id="my-agent", model="gemini-3.5-pro")
llm = GeminiProvider(app_id="my-agent", web_search=True)            # Gemini + native grounding
```

OpenAI `gpt-5*` are reasoning models: they ignore `temperature` (the provider
drops it), and the runtime reserves part of the output budget for hidden
reasoning tokens — keep `AgentConfig.max_tokens` generous (OpenAI recommends
≥ 25,000) or responses may truncate before the final answer.

`web_search=True` injects Gemini's native `google_search` grounding tool into
every request, giving grounded responses from `GEMINI_API_KEY` alone — no
`WebSearchProvider`, no MCP, no extra key. This is independent of any provider
returned from `build_web_search()`: use `web_search=True` for Gemini-native
grounding, or `ParallelSearchProvider` (see [Web Search](#web-search)) for the
provider-agnostic `web_search`/`web_fetch` tools that work across any LLM.

API keys are read from environment variables: `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). You can also pass
`api_key=` explicitly.

### Open-source models

`OSSCompatibleProvider` runs open-source models through the same harness over any
OpenAI Chat Completions endpoint: self-hosted with vLLM, Ollama, or llama.cpp, or
a hosted gateway like OpenRouter, Together, or Groq. Mash is the client; you run
or pay for the endpoint. The model must be served with native tool calling so the
runtime can pass `tools=` and read back `message.tool_calls`; the latest Gemma,
Qwen, DeepSeek, and Llama releases qualify. On a hosted gateway, pick a model
whose route supports tool use.

The presets `GemmaProvider`, `QwenProvider`, `DeepSeekProvider`, and
`LlamaProvider` pin a default model and a capability profile. Pass `base_url`,
and an `api_key` for a gateway:

```python
from mash.core.llm import (
    OSSCompatibleProvider, GemmaProvider, QwenProvider, DeepSeekProvider, LlamaProvider,
)

# Self-hosted with Ollama on localhost (no key needed)
llm = QwenProvider(app_id="my-agent", base_url="http://localhost:11434/v1")

# Self-hosted with vLLM on a GPU box, explicit model
llm = GemmaProvider(app_id="my-agent", model="google/gemma-4-27b-it",
                    base_url="http://gpu-box:8000/v1")

# Hosted gateway with a key
import os
llm = OSSCompatibleProvider(
    app_id="my-agent",
    model="deepseek/deepseek-chat",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
```

`base_url` falls back to `OSS_BASE_URL` (default `http://localhost:11434/v1`,
Ollama); the key falls back to `OSS_API_KEY`, or a placeholder when a self-hosted
engine needs none. Each preset's default model comes from `GEMMA_MODEL`,
`QWEN_MODEL`, `DEEPSEEK_MODEL`, or `LLAMA_MODEL`.

## Step 4: Configure Agent Behavior

`AgentConfig` carries every behavior knob. `app_id` and `system_prompt` are
required; the rest have the defaults shown here.

```python
AgentConfig(
    app_id="my-agent",                    # required, must match get_agent_id()
    system_prompt="You are ...",          # required, str or list of content blocks
    max_steps=30,                         # max tool-use loops per request
    max_tokens=4096,                      # LLM output token cap per response
    temperature=1.0,                      # sampling temperature
    skills_enabled=True,                  # set False to disable the Skill meta-tool
    prompt_caching_enabled=True,          # cache the system prompt + tools with the provider
    streaming_enabled=True,               # stream tokens and emit llm.response.delta events
    conversation_history_turns=3,         # prior turns replayed into context
    compaction_token_threshold=0,         # summarize history past this token count (0 = off)
    compaction_turn_limit=50,             # how many recent turns the summary keeps when compaction runs
    compaction_temperature=0.0,           # sampling temperature for the summary pass
    extra={},                             # free-form dict for provider/app-specific options
)
```

Compaction is off by default; set `compaction_token_threshold` to a positive
value to have long sessions summarized automatically.

### System Prompt Tips

- Be specific about the agent's role, capabilities, and boundaries.
- List available tools and when to use each one.
- Define output format expectations if applicable.

## Step 5: Multi-Agent Composition (if needed)

Register every agent role-less into the flat pool, then compose a `Host`
over it. Roles (primary, subagents) live in the host, not on the agents:

```python
from mash.runtime import AgentMetadata, Host, HostBuilder

pool = (
    HostBuilder()
    .agent(AssistantAgent(), metadata=AgentMetadata(...))
    .agent(
        ResearchAgent(),
        metadata=AgentMetadata(
            display_name="Research Agent",
            description="Handles deep research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Delegate research-heavy questions here.",
        ),
    )
    .host(
        Host(
            host_id="assistant",
            primary="assistant",
            subagents=("research",),
        )
    )
    .build()
)
```

### How host routing works

Submitting a request to a host routes it to that host's primary and — for
that request only — wires the primary with an `InvokeSubagent` tool plus a
directory of the host's subagents, built from their `AgentMetadata`. The
primary's model reads that directory to decide when to delegate, so
**delegation quality is a prompt-engineering surface**: vague
`usage_guidance` produces vague routing. A bare request to the same agent
(`POST /v1/agent/{agent_id}/request`) gets no directory and no delegation
tool — the agent answers alone.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/hosts/assistant/request \
  -H "Content-Type: application/json" \
  -d '{"message": "find recent papers", "session_id": "s-1"}'
# -> {"request_id": "...", "agent_id": "assistant", "session_id": "s-1"}
```

The response names the primary `agent_id`; stream results from the existing
`GET /v1/agent/{agent_id}/request/{request_id}/events`. Each request
snapshots the host composition at submit time, so redefining a host never
affects in-flight requests.

### Code-defined vs dynamic hosts

A host is just data (a few agent ids), so there are two ways to define one:

- **Code-defined** (`.host(Host(...))` as above) — ships with the deploy and
  is re-created on every restart. Use this for compositions that must always
  exist, e.g. when other clients target the deployment.
- **Dynamic** — defined on a running pool, in code with
  `pool.define_host(Host(host_id="research-only", primary="research"))` or
  over the API with an idempotent `PUT`:

  ```bash
  curl -X PUT http://127.0.0.1:8000/api/v1/hosts/research-only \
    -H "Content-Type: application/json" \
    -d '{"primary": "research", "subagents": [], "workflows": []}'
  ```

  Dynamic hosts are in-memory: they disappear on restart and must be
  re-`PUT` (the PUT is idempotent, so clients can safely define their
  composition on every startup). If a composition references an agent that
  isn't in the pool, the server rejects it with a clear error.

Because hosts are cheap, a client can compose a host per task, route a few
requests through it, and forget it.

## Step 6: MCP Server Integration (if needed)

Connect external MCP servers to give the agent additional tools:

```python
from mash.mcp.types import MCPServerConfig

class MyAgent(AgentSpec):
    def build_mcp_servers(self):
        return [
            MCPServerConfig(
                name="my-server",
                url="http://localhost:3000/sse",
                description="Custom MCP server for domain-specific tools",
            ),
        ]
```

## Step 7: Workflows (if needed)

For ordered multi-step pipelines:

```python
from mash.workflows import TaskSpec, WorkflowSpec

workflow = WorkflowSpec(
    workflow_id="my-pipeline",
    tasks=[
        TaskSpec(task_id="step-1", agent_spec=Step1Agent()),
        TaskSpec(task_id="step-2", agent_spec=Step2Agent()),
    ],
)

pool = (
    HostBuilder()
    .agent(AssistantAgent(), metadata=AgentMetadata(...))
    .workflow(workflow)
    .build()
)
```

Specs registered through `.workflow(...)` (or `pool.register_workflow_agent`)
become **workflow-only agents**: full runtimes that execute workflow tasks
but are hidden from public agent listings and can't be named in a host —
primaries can't delegate to them and clients can't address them directly.

### Typed task output

A `TaskSpec` can pin a JSON-schema `structured_output`, so the task's final
turn is validated and returned as a `structured_output` object on the
`request.completed` event (alongside the usual `text`):

```python
TaskSpec(
    task_id="summarize",
    agent_spec=SummaryAgent(),
    structured_output={
        "title": "SummaryResult",
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "bullets"],
        "additionalProperties": False,
    },
)
```

This is the durable, workflow-level counterpart to the request-scoped
[Structured Output](#structured-output) below; a CLI can register a renderer
for the workflow id (`shell.register_structured_output_renderer`) to display
the typed payload instead of raw JSON.

## Step 8: Run the Agent

```bash
# Start the host server
mash host serve --host-app {project_name}.spec:build_pool --port 8000

# Single agent: connect straight to it (bare mode, no delegation)
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent assistant

# Multi-agent: connect, inspect the pool, compose a host, and pin a REPL to it
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret
mash agents                                          # what's in the pool
mash compose --host assistant --primary assistant --subagents research
mash repl                                            # routed through 'assistant'
```

`mash compose` issues the idempotent `PUT /v1/hosts/{host_id}` and pins later
commands to that host; `mash hosts` lists defined compositions. The REPL
target is fixed for its lifetime — exit and `mash compose` again to change
composition. With a code-defined host (`.host(...)` in `build_pool()`), skip
`mash compose` and `mash repl` against the shipped host directly.

Or run programmatically:

```python
from mash.api import run_host, MashHostConfig

run_host(
    build_pool(),
    config=MashHostConfig(bind_host="0.0.0.0", bind_port=8000, api_key="secret"),
)
```

For a complete guide on building your own CLI for a Mash deployment, read
`docs/posts/building-agent-clis.md`. For how composition works under the
hood (delegation, per-request role wiring, mirrored traces), read
`docs/posts/composing-agents.md`; for driving composition purely over HTTP,
`docs/posts/building-dynamic-hosts-apis.md`.

## Collecting Feedback

The REPL ships a `/feedback` command. A user types a note or bug report and it
is saved with the session context, no LLM step involved:

```bash
mash repl
› /feedback the trace output is hard to read
✓ Feedback recorded (session s-1, request r-9)
```

The message lands in a `runtime_feedback` table in the runtime store, tagged
with the host, agent, session, and last request id from that shell. Read it
back over the API to inspect reports across sessions:

```bash
# after is a required unix timestamp lower bound; pass 0 to read from the start
curl "http://127.0.0.1:8000/api/v1/feedback?agent_id=assistant&after=0"

# narrow it: full-text q over the message, plus before / session_id / feedback_type / limit
curl "http://127.0.0.1:8000/api/v1/feedback?agent_id=assistant&after=0&q=trace"
```

`POST /api/v1/feedback` records feedback programmatically (`agent_id` and
`message` required). Neither route depends on `enable_observability`.

## Structured Output

To get typed JSON responses from agents:

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
```

## Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Google Gemini API key |
| `OSS_BASE_URL` | Default Chat Completions endpoint for OSS providers (default `http://localhost:11434/v1`, Ollama) |
| `OSS_API_KEY` | Key for a hosted OSS gateway (self-hosted engines need none) |
| `GEMMA_MODEL` / `QWEN_MODEL` / `DEEPSEEK_MODEL` / `LLAMA_MODEL` | Default model id for each OSS preset |
| `PARALLEL_API_KEY` | Parallel AI key for web search (optional; free tier needs none) |
| `PARALLEL_OAUTH_TOKEN` | Parallel AI OAuth token for web search (optional) |
| `MASH_DATABASE_URL` | Postgres URL for memory/runtime stores |
| `MASH_DATA_DIR` | Persistent data directory (default: `/var/lib/mash`) |
| `MASH_API_KEY` | API key for the hosted server |

## Deployment

For deploying a Mash Host (local, Docker, cloud, horizontal scaling), read
`docs/posts/how-to-deploy.md`.

## Reference Documentation

Read these local files (relative to the workspace root) when you need deeper
context on a specific subsystem:

- Package overview: `src/mash/README.md`
- Runtime & hosting: `src/mash/runtime/README.md`
- Tools: `src/mash/tools/README.md`
- Skills: `src/mash/skills/README.md`
- LLM providers: `src/mash/core/llm/README.md`
- Workflows: `src/mash/workflows/README.md`
- API server: `src/mash/api/README.md`
- CLI: `src/mash/cli/README.md`
- Memory: `src/mash/memory/README.md`
- MCP: `src/mash/mcp/README.md`

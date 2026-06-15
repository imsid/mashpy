# Mash — Building Agents

Mash is a Python SDK and host runtime for building self-hosted multi-agent
applications. This file gives coding agents (Claude Code, Codex, Cursor, etc.)
everything they need to scaffold and build a Mash-powered agent from a user
prompt.

## Install

```bash
pip install mashpy
```

Requires Python >= 3.10. Set environment variables for the LLM provider you
plan to use: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`.

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
  `AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`.
- **WorkflowSpec / TaskSpec** — ordered task chains orchestrated by DBOS.

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
| `build_memory_store()` | Auto (Postgres or SQLite) | Custom memory backend |
| `build_mcp_servers()` | `[]` | MCP server connections |
| `enable_runtime_tools()` | `True` | Auto-register runtime tools |
| `enable_web_search_tools()` | `False` | Auto-register `web_search`/`web_fetch` |
| `build_web_search()` | Parallel when enabled | Web search provider |
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

### Built-in Tools

```python
from mash.tools.bash import BashTool
from mash.tools.ask_user import AskUserTool

tools = ToolRegistry()
tools.register(BashTool(working_dir="/path/to/workspace"))
tools.register(AskUserTool())  # only works in hosted runtime
```

### Web Search

Give an agent `web_search` and `web_fetch` by flipping one method. It's off by
default because the tools hit the network and the authenticated tier needs a
key. The default provider is Parallel AI, which has a free no-auth tier.

```python
class ResearchAgent(AgentSpec):
    def enable_web_search_tools(self) -> bool:
        return True
```

That uses the free tier. To raise the limits, pass a key or an OAuth token. The
provider reads `PARALLEL_API_KEY` and `PARALLEL_OAUTH_TOKEN` from the
environment, or you can pass them in directly:

```python
from mash.tools.web_search import ParallelSearchProvider

class ResearchAgent(AgentSpec):
    def enable_web_search_tools(self) -> bool:
        return True

    def build_web_search(self):
        return ParallelSearchProvider(api_key="...")  # or oauth_token="..."
```

A token (key or OAuth) is sent as `Authorization: Bearer <token>`; there is no
interactive OAuth2 flow. The tools register under their plain names —
`web_search` and `web_fetch` — and ride the same path as remote MCP tools.

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

```python
from mash.workflows import TaskSpec, WorkflowSpec

workflow = WorkflowSpec(
    workflow_id="my-pipeline",
    tasks=[
        TaskSpec(task_id="step-1", agent_spec=Step1AgentSpec()),
        TaskSpec(task_id="step-2", agent_spec=Step2AgentSpec()),
    ],
)

pool = (
    HostBuilder()
    .agent(ConciergeAgent(), metadata=AgentMetadata(...))
    .workflow(workflow)
    .build()
)
```

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
- [API server](https://github.com/imsid/mashpy/blob/main/src/mash/api/README.md)
- [CLI](https://github.com/imsid/mashpy/blob/main/src/mash/cli/README.md)
- [Memory](https://github.com/imsid/mashpy/blob/main/src/mash/memory/README.md)
- [MCP integration](https://github.com/imsid/mashpy/blob/main/src/mash/mcp/README.md)
- [Pilot example app](https://github.com/imsid/mash-pilot)

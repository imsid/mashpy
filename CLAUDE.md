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
- **HostBuilder** — fluent builder that composes a primary agent, optional
  subagents, and optional workflows into an `AgentHost`.
- **AgentHost** — the composed multi-agent host that the API server runs.
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
from mash.runtime import AgentSpec, HostBuilder
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry


class PrimaryAgent(AgentSpec):
    def get_agent_id(self) -> str:
        return "primary"

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="primary")

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id="primary",
            system_prompt="You are a helpful assistant.",
        )


def build_host():
    return HostBuilder().primary(PrimaryAgent()).build()
```

Run it:

```bash
mash host serve --host-app my_agent.spec:build_host --port 8000
```

Connect:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent primary
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
| `on_startup(runtime)` | No-op | Hook after runtime init |
| `on_shutdown(runtime)` | No-op | Hook before cleanup |

## AgentConfig Fields

```python
AgentConfig(
    app_id="my-agent",                    # required, matches agent id
    system_prompt="You are ...",          # required, str or list of blocks
    max_steps=30,                         # max tool-use loops per request
    max_tokens=4096,                      # LLM output token cap
    temperature=1.0,                      # sampling temperature
    skills_enabled=False,                 # enable the Skill meta-tool
    prompt_caching_enabled=True,          # provider prompt caching
    streaming_enabled=True,               # stream tokens + emit llm.response.delta events
    conversation_history_turns=3,         # turns of history in context
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

```python
from mash.runtime import HostBuilder, SubAgentMetadata

host = (
    HostBuilder()
    .primary(PrimaryAgent())
    .subagent(
        ResearchAgent(),
        metadata=SubAgentMetadata(
            display_name="Research Agent",
            description="Handles research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Use for research-heavy questions.",
        ),
    )
    .subagent(CodeAgent(), metadata=SubAgentMetadata(...))
    .build()
)
```

The primary agent gets an `InvokeSubagent` tool automatically and can
delegate to registered subagents.

**Connection sharing:** `AgentHost` creates one shared Postgres connection
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

host = HostBuilder().primary(PrimaryAgent()).workflow(workflow).build()
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
mash host serve --host-app my_agent.spec:build_host --port 8000

# Docker (using the Mash base image)
# Set MASH_HOST_APP, MASH_DATA_DIR, MASH_DATABASE_URL

# Programmatic
from mash.api import run_host, MashHostConfig
run_host(host, config=MashHostConfig(bind_host="0.0.0.0", bind_port=8000))
```

For full deployment instructions (Docker Compose, horizontal scaling,
cloud deployment, and external API access), see
[HOW_TO_DEPLOY.md](docs/HOW_TO_DEPLOY.md).

## Project Structure Convention

```
my_agent/
  __init__.py
  spec.py          # AgentSpec subclasses + build_host()
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

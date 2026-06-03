---
name: build-mash-agent
description: Scaffold and build a Mash-powered agent application from a user prompt.
---

# Build a Mash Agent

You are helping a developer build an agent application using the **Mash** Python
SDK (`pip install mashpy`). Mash is a framework for building self-hosted
multi-agent applications with durable execution, human-in-the-loop interactions,
and a built-in API server.

Follow the steps below to scaffold a working Mash agent from the user's
description.

## Step 1: Gather Requirements

From the user's prompt, determine:

1. **Agent purpose** — what the agent does (e.g., "customer support bot",
   "code reviewer", "data pipeline orchestrator")
2. **Tools needed** — what actions the agent can take (bash, web search,
   database queries, API calls, file operations, etc.)
3. **LLM provider** — Anthropic (default), OpenAI, or Google Gemini
4. **Multi-agent** — does the user need subagents for delegation?
5. **Workflows** — does the user need ordered task pipelines?
6. **Human-in-the-loop** — does any tool need user approval before executing?

If the user's prompt is ambiguous, make reasonable defaults and note them.

## Step 2: Scaffold the Project

Create this file structure:

```
{project_name}/
  __init__.py
  spec.py          # AgentSpec subclass(es) + build_host()
  tools.py         # Custom tool implementations (if any)
```

### spec.py — The Agent Definition

Every Mash agent implements `AgentSpec`. Here is the minimal scaffold:

```python
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentSpec, HostBuilder
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry


class PrimaryAgent(AgentSpec):
    def get_agent_id(self) -> str:
        return "primary"

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        # Register tools here
        return tools

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

### Tool Approval

Set `requires_approval = True` on any tool that should pause for user consent
before executing. The runtime handles the approval flow automatically — no
additional wiring needed.

## Step 3: Choose an LLM Provider

```python
from mash.core.llm import AnthropicProvider, OpenAIProvider, GeminiProvider

# Pick one:
llm = AnthropicProvider(app_id="my-agent")                          # Claude
llm = AnthropicProvider(app_id="my-agent", model="claude-sonnet-4-6-20250514")

llm = OpenAIProvider(app_id="my-agent")                             # GPT
llm = OpenAIProvider(app_id="my-agent", model="gpt-5")

llm = GeminiProvider(app_id="my-agent")                             # Gemini
llm = GeminiProvider(app_id="my-agent", model="gemini-2.5-pro")
```

API keys are read from environment variables: `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). You can also pass
`api_key=` explicitly.

## Step 4: Configure Agent Behavior

```python
AgentConfig(
    app_id="my-agent",
    system_prompt="You are ...",          # required — defines agent personality
    max_steps=30,                         # max tool-use loops per request
    max_tokens=4096,                      # LLM output token cap
    temperature=1.0,                      # sampling temperature
    skills_enabled=False,                 # set True to enable Skill meta-tool
    conversation_history_turns=3,         # how many prior turns to include
)
```

### System Prompt Tips

- Be specific about the agent's role, capabilities, and boundaries.
- List available tools and when to use each one.
- Define output format expectations if applicable.
## Step 5: Multi-Agent Composition (if needed)

```python
from mash.runtime import HostBuilder, SubAgentMetadata

host = (
    HostBuilder()
    .primary(PrimaryAgent())
    .subagent(
        ResearchAgent(),
        metadata=SubAgentMetadata(
            display_name="Research Agent",
            description="Handles deep research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Delegate research-heavy questions here.",
        ),
    )
    .build()
)
```

The primary agent automatically gets an `InvokeSubagent` tool for delegation.

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

host = HostBuilder().primary(PrimaryAgent()).workflow(workflow).build()
```

## Step 8: Run the Agent

```bash
# Start the host server
mash host serve --host-app {project_name}.spec:build_host --port 8000

# Connect the CLI (in another terminal)
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent primary
```

Or run programmatically:

```python
from mash.api import run_host, MashHostConfig

run_host(
    build_host(),
    config=MashHostConfig(bind_host="0.0.0.0", bind_port=8000, api_key="secret"),
)
```

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
| `MASH_DATABASE_URL` | Postgres URL for memory/runtime stores |
| `MASH_DATA_DIR` | Persistent data directory (default: `/var/lib/mash`) |
| `MASH_API_KEY` | API key for the hosted server |

## Reference Documentation

Fetch these URLs when you need deeper context on a specific subsystem:

- Package overview: https://github.com/imsid/mashpy/blob/main/src/mash/README.md
- Runtime & hosting: https://github.com/imsid/mashpy/blob/main/src/mash/runtime/README.md
- Tools: https://github.com/imsid/mashpy/blob/main/src/mash/tools/README.md
- Skills: https://github.com/imsid/mashpy/blob/main/src/mash/skills/README.md
- LLM providers: https://github.com/imsid/mashpy/blob/main/src/mash/core/llm/README.md
- Workflows: https://github.com/imsid/mashpy/blob/main/src/mash/workflows/README.md
- API server: https://github.com/imsid/mashpy/blob/main/src/mash/api/README.md
- CLI: https://github.com/imsid/mashpy/blob/main/src/mash/cli/README.md
- Memory: https://github.com/imsid/mashpy/blob/main/src/mash/memory/README.md
- MCP: https://github.com/imsid/mashpy/blob/main/src/mash/mcp/README.md
- Example app (Pilot): https://github.com/imsid/mashpy/blob/main/pilot/README.md

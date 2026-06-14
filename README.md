# Mash

A Python SDK and host runtime for building self-hosted multi-agent applications.

Mash gives you a Python `AgentSpec` contract for defining agents, a `HostBuilder`
for composing them into a multi-agent host, a FastAPI server for deployment, and
a CLI/API for interacting with a running host.

It's designed around [Host-to-Agent Protocol (H2A)](rfcs/host-to-agent-protocol.md) that 
standardizes interactions between user applications and agents. 

## What Mash Provides

- **Multi-agent composition** — define a primary agent, add specialized subagents,
  and compose workflows behind a single host. Agents delegate to each other
  without a separate coordination layer.
- **Durable harness** — requests execute through a durable engine and are recorded
  as replayable runtime events. Retries, restarts, and long-running work just
  work.
- **Human-in-the-loop** — agents can pause for approval or ask users questions
  mid-execution. Interactions survive host restarts.
- **Workflows** — ordered task sequences with structured output, defined in code
  or published dynamically at runtime.
- **Observability** — span trees, trace analysis, telemetry API, built-in
  dashboard, and CLI trace inspection. No external APM needed.
- **Self-hosted interfaces** — HTTP API with streaming, CLI, and interactive REPL.
  Deploy locally, in Docker, or on any cloud.

```
                  ┌─────────────────────────────────────────┐
                  │          Durable Request                │
                  │                                         │
                  │   ┌─ context ─── memory ──┐             │
                  │   │                       │             │
request ────────► │   │     Agent Loop        │ ──► signals │
(cli/api)         │   │ think → act → observe │      │      │
                  │   │                       │      ▼      │
                  │   └─ tools ───── skills ──┘  structured │
workflow ───────► │        ▲                      output    │
(schedule/trigger)│        │ user interaction               │
                  │        ▼ (approval / ask-user)          │
                  │                                         │
                  │       resumable · replayable            │
                  └─────────────────────────────────────────┘
```

See [Mash under the hood](docs/posts/mash-under-the-hood.md) for a deeper look
at each capability, and the [product brief](docs/posts/product-brief.md) for
the pitch.

## Quick Start

**Install:**

```bash
# install the library
uv add mashpy

# install the `mash` CLI on your PATH
uv tool install mashpy 
```

**Define your agents:**

Each agent is an `AgentSpec` subclass. It names itself, picks an LLM, and
declares a system prompt, tools, skills and agent config.

```python
## my_app/agents.py

from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentSpec
from mash.skills import SkillRegistry
from mash.tools import ToolRegistry


class ConciergeAgent(AgentSpec):
    def get_agent_id(self):
        return "concierge"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="concierge")

    def build_agent_config(self):
        return AgentConfig(
            app_id="concierge",
            system_prompt=(
                "You are the concierge. Answer the user directly, and "
                "delegate research-heavy questions to the research subagent."
            ),
        )


class ResearchAgent(AgentSpec):
    def get_agent_id(self):
        return "research"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="research")

    def build_agent_config(self):
        return AgentConfig(
            app_id="research",
            system_prompt="You handle research-heavy questions in depth.",
        )
```

**Build Mash host with an Agent pool:**

```python
## my_app/host.py

from mash.runtime import AgentMetadata, Host, HostBuilder

from .agents import ConciergeAgent, ResearchAgent

def build_pool():
  pool = (
      HostBuilder()
      .agent(
          ConciergeAgent(),
          metadata=AgentMetadata(
              display_name="Concierge",
              description="Front-door agent that answers users and delegates.",
              capabilities=["conversation", "delegation"],
              usage_guidance="Default entry point for user requests.",
          ),
      )
      .agent(
          ResearchAgent(),
          metadata=AgentMetadata(
              display_name="Research",
              description="Handles research-heavy questions in depth.",
              capabilities=["research", "analysis"],
              usage_guidance="Use for questions that need digging.",
          ),
      )
      .build()
  )
  return pool
```

**Configure the environment:**

The host needs an LLM key and a Postgres URL for its durable runtime. Put
them in a `.env` file the host loads on start:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
MASH_DATABASE_URL=postgresql://user:pass@localhost:5432/mash
```

**Start the host:**

```bash
mash host serve --host-app my_app.host:build_pool --host 127.0.0.1 --port 8000
```

**Browse available agents:**
```bash
mash browse
```

**Compose an assistant host with primary and subagents:**
```bash
mash compose assistant --primary concierge --subagents research
```

**Talk to the host or execute `/` commands using Mash repl:**
```bash
mash repl --host assistant
```

## Key Concepts

| Concept | What it is |
|---|---|
| **AgentSpec** | Abstract contract defining one agent (id, tools, skills, LLM, config) |
| **HostBuilder** | Fluent builder that composes agents, workflows, and hosts into an AgentPool |
| **AgentPool** | The deployed pool of role-less agents the API server runs |
| **Host** | A composition over the pool (primary + subagents + workflows), defined in code or dynamically over the API |
| **ToolRegistry** | Register callable tools; built-ins include Bash, AskUser, InvokeSubagent |
| **SkillRegistry** | Markdown instruction bundles loaded on demand via a meta-tool |
| **LLMProvider** | Adapters for Anthropic, OpenAI, and Gemini |
| **WorkflowSpec** | Ordered task chains with structured output, orchestrated by DBOS |

## Example: Mash Pilot

[Mash Pilot](https://github.com/imsid/mash-pilot) is a full example host app
built on the Mash SDK. It demonstrates multi-agent composition, custom REPL
commands, workflows, and deployment. Use it as a reference when building your
own host.

## Build with a Coding Agent

This repo includes [`CLAUDE.md`](CLAUDE.md) so coding agents like Claude Code,
Codex, and Cursor can scaffold a Mash-powered agent from a natural language
prompt. Copy it into your project or point your agent at this repo to get
started. The [Pilot](https://github.com/imsid/mash-pilot) agent also includes
a `build-mash-agent` skill for interactive agent scaffolding.

## Documentation

- [Product brief](docs/posts/product-brief.md) — the pitch: why the application-to-agent seam needs a standard
- [Mash under the hood](docs/posts/mash-under-the-hood.md) — what Mash offers and where it fits
- [Deployment guide](docs/posts/how-to-deploy.md) — Docker, cloud, horizontal scaling
- [Building agent CLIs](docs/posts/building-agent-clis.md) — custom CLI development
- [CLAUDE.md](CLAUDE.md) — full SDK reference for coding agents
- [Package overview](src/mash/README.md) — subsystem boundaries and module guides
- [Contributing](CONTRIBUTING.md) — development setup, tests, repo structure

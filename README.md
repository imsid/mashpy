# Mash

A Python SDK and host runtime for building self-hosted multi-agent applications.

Mash gives you a Python `AgentSpec` contract for defining agents, a `HostBuilder`
for composing them into a multi-agent host, a FastAPI server for deployment, and
a CLI/REPL for interacting with a running host.

## Install

```bash
pip install mashpy
```

Requires Python >= 3.10. Set your LLM provider key:

```bash
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY or GEMINI_API_KEY
```

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

See the [product brief](docs/product-brief.md) for a deeper look at each
capability.

## Quick Start

### 1. Define an agent

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

### 2. Serve it

```bash
mash host serve --host-app my_agent.spec:build_host --port 8000
```

### 3. Connect

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent primary
mash repl
```

## Key Concepts

| Concept | What it is |
|---|---|
| **AgentSpec** | Abstract contract defining one agent (id, tools, skills, LLM, config) |
| **HostBuilder** | Fluent builder that composes agents and workflows into an AgentHost |
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

- [Product brief](docs/product-brief.md) — what Mash offers and where it fits
- [Deployment guide](docs/how-to-deploy.md) — Docker, cloud, horizontal scaling
- [Building agent CLIs](docs/building-agent-clis.md) — custom CLI development
- [CLAUDE.md](CLAUDE.md) — full SDK reference for coding agents
- [Package overview](src/mash/README.md) — subsystem boundaries and module guides
- [Contributing](CONTRIBUTING.md) — development setup, tests, repo structure

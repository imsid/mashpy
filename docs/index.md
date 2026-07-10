---
title: Mash
description: Build self-hosted agent applications on frontier and open source models.
hide:
  - navigation
  - toc
---

# Mash

**Build self-hosted agent applications on frontier and open source models.**

[Mash](posts/product-brief.md) is a Python SDK and a host runtime for building,
composing, and evaluating agents and workflows.
It's designed around [Host-to-Agent Protocol (H2A)](rfcs/host-to-agent-protocol.md) that 
standardizes interactions between user applications and agents. 

It runs on both frontier and open source models. The harness includes web search,
local and remote (MCP) tools, skills, context and memory management,
human-in-the-loop (HITL), pre-built commands and workflows, synthetic evals,
an API/CLI for access, observability, and a durable runtime.

**Try it now with Pilot:**

[Pilot](https://github.com/imsid/mash-pilot) is a ready-made Mash host runtime composed
of multiple agents that answer questions about the Mash codebase and guide you through
building your own agent application. One container, no code to write — good for getting
a feel for the runtime before building your own.

```bash
# 1. Start the host (embedded Postgres, Mash source included)
docker run -d --name pilot -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v pilot-data:/var/lib/pilot \
  ghcr.io/imsid/mash-pilot:latest

# 2. Install the CLI and start asking
curl -fsSL https://raw.githubusercontent.com/imsid/mash-pilot/main/install.sh | sh
pilot repl --host guide
```

Add `-e OPENAI_API_KEY=sk-...` instead if you prefer OpenAI. The `pilot-data` volume
keeps your database durable across restarts. Once you're ready to build your own agent,
keep reading.

---

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

**Add a workflow:**

A workflow is an ordered pipeline of typed steps. Use a `CodeStep` for
deterministic Python and an `AgentStep` when the work needs an agent.

```python
## my_app/workflows.py

from pydantic import BaseModel

from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec


class ResearchRequest(BaseModel):
    topic: str


class ResearchPlan(BaseModel):
    topic: str
    questions: list[str]


class ResearchBrief(BaseModel):
    summary: str
    sources: list[str]


def plan_research(
    request: ResearchRequest,
    _context: StepContext,
) -> ResearchPlan:
    return ResearchPlan(
        topic=request.topic,
        questions=[
            f"What are the key facts about {request.topic}?",
            f"What should a reader understand about {request.topic}?",
        ],
    )


RESEARCH_BRIEF = WorkflowSpec(
    workflow_id="research-brief",
    input_model=ResearchRequest,
    steps=[
        CodeStep(
            step_id="plan",
            run=plan_research,
            input=ResearchRequest,
            output=ResearchPlan,
        ),
        AgentStep(
            step_id="research",
            agent_id="research",
            input=ResearchPlan,
            output=ResearchBrief,
        ),
    ],
)
```

The `CodeStep` output becomes the `AgentStep` input. Mash validates both edges,
runs each step durably, and uses the last step's output as the workflow result.

**Build Mash host with an Agent pool:**

```python
## my_app/host.py

from mash.runtime import AgentMetadata, HostBuilder

from .agents import ConciergeAgent, ResearchAgent
from .workflows import RESEARCH_BRIEF


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
        .workflow(RESEARCH_BRIEF)
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
mash compose assistant --primary concierge --subagents research \
  --workflows research-brief
```

**Talk to the host or execute `/` commands using Mash repl:**
```bash
mash repl --host assistant
```

## Learn More

- [**General concepts**](posts/concepts.md): the building blocks behind every agentic platform
- [**Product brief**](posts/product-brief.md): the pitch, why applications and agents needs a standard
- [**Mash under the hood**](posts/mash-under-the-hood.md): what Mash provides and where it fits
- [**H2A Protocol RFC**](rfcs/host-to-agent-protocol.md): the host-to-agent boundary

## Internals

- [**Life of a Mash request**](posts/request-lifecycle.md): follow one message from POST to `request.completed`
- [**Durable agent loop**](posts/durable-agent-loop.md): checkpoints, retries, and surviving `kill -9`
- [**Persistence store**](posts/persistence-store.md): the event log, feedback, and memory tables a request touches
- [**Host API and CLI**](posts/host-api-and-cli.md): the HTTP surface applications integrate with, and the REPL built on it
- [**Human-in-the-loop**](posts/human-in-the-loop.md): tool approval and ask-user on one durable mechanism
- [**Remote tools over MCP**](posts/remote-tools-mcp.md): connecting MCP servers and their tools
- [**One LLM contract**](posts/one-llm-contract.md): normalized requests, caching, streaming
- [**Open-source models**](posts/oss-models.md): running Gemma, Qwen, and DeepSeek on the same harness, self-hosted or hosted
- [**Skills: Instructions on demand**](posts/skills-on-demand.md): markdown bundles loaded through one meta-tool
- [**Memory and compaction**](posts/memory-and-compaction.md): history, summary checkpoints, and signals
- [**Composing agents**](posts/composing-agents.md): subagents, delegation, shared plumbing
- [**Workflows**](posts/workflows-as-step-pipelines.md): durable step pipelines mixing code steps and agent steps
- [**Reading a trace**](posts/reading-a-trace.md): events to spans to latency answers
- [**Synthetic evals**](posts/synthetic-evals.md): generated datasets and rubrics, experiments over the live host, read-time comparison

## Guides

- [**Exploring Mash with Pilot**](posts/exploring-mash-with-pilot.md): ask the Pilot CLI questions about the Mash codebase
- [**Building an agent CLI**](posts/building-agent-clis.md): custom CLI development with dynamic host composition
- [**Building dynamic hosts over the API**](posts/building-dynamic-hosts-apis.md): compose agent teams at runtime over plain HTTP
- [**Deploying a Mash Host**](posts/how-to-deploy.md): laptop, Docker, and cloud
- [**Release process**](posts/releasing.md): how versioning, changelogs, and releases work

## Learnings

- [**Prompt caching and the token meter**](posts/prompt-caching-token-meter.md): what an eval fan-out pays on a cold cache, read from real scoring runs

## Applications

- [**Pilot CLI**](https://github.com/imsid/mashpy/src/pilot) - a multi-agent CLI guide to build and deploy Mash agents.
- [**PA CLI**](https://github.com/imsid/mash-pa) - a self-hosted store of personal agents that help run your day.
- [**Crew**](https://github.com/imsid/crew) - a self-hosted crew of role-based agents for analytics and product work, with a CLI and a web UI.

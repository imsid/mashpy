---
title: Mash
description: A self-hosted runtime and SDK for building multi-agent applications.
hide:
  - navigation
  - toc
---

# Mash

**Build self-hosted, multi-agent applications.**

Mash is a Python SDK and a host runtime for composing agents. 
It's designed around [Host-to-Agent Protocol (H2A)](rfcs/host-to-agent-protocol.md) that 
standardizes interactions between user applications and agents. 
Agents are model agnostic and come with powerful built-in tools including web search, plus skills, memory, observability, and a API/CLI for access.

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

## Start here

- [**Product brief**](posts/product-brief.md): the pitch, why applications and agents needs a standard
- [**Mash under the hood**](posts/mash-under-the-hood.md): what Mash provides and where it fits
- [**H2A Protocol RFC**](rfcs/host-to-agent-protocol.md): the host-to-agent boundary

## Internals

- [**The life of a Mash request**](posts/request-lifecycle.md): follow one message from POST to `request.completed`
- [**The durable agent loop**](posts/durable-agent-loop.md): checkpoints, retries, and surviving `kill -9`
- [**Two stores**](posts/two-stores.md): the event log vs. conversation memory
- [**Human-in-the-loop**](posts/human-in-the-loop.md): tool approval and ask-user on one durable mechanism
- [**Remote tools over MCP**](posts/remote-tools-mcp.md): connecting MCP servers and their tools
- [**One LLM contract**](posts/one-llm-contract.md): normalized requests, caching, streaming
- [**Skills: instructions on demand**](posts/skills-on-demand.md): markdown bundles loaded through one meta-tool
- [**Memory and compaction**](posts/memory-and-compaction.md): history, summary checkpoints, and signals
- [**Composing agents under one host**](posts/composing-agents.md): subagents, delegation, shared plumbing
- [**Workflows**](posts/workflows-and-task-state.md): code authored and dynamic specs for deterministic tasks
- [**The host API and CLI**](posts/host-api-and-cli.md): the HTTP surface applications integrate with, and the REPL built on it
- [**Reading a trace**](posts/reading-a-trace.md): events to spans to latency answers

## Guides

- [**Exploring Mash with Pilot**](posts/exploring-mash-with-pilot.md): ask the Pilot CLI questions about the Mash codebase
- [**Building an agent CLI**](posts/building-agent-clis.md): custom CLI development with dynamic host composition
- [**Building dynamic hosts over the API**](posts/building-dynamic-hosts-apis.md): compose agent teams at runtime over plain HTTP
- [**Deploying a Mash Host**](posts/how-to-deploy.md): laptop, Docker, and cloud

## Applications

- [**Pilot CLI**](https://github.com/imsid/mash-pilot) - a multi-agent CLI guide to build and deploy Mash agents.
- [**PA CLI**](https://github.com/imsid/mash-pa) - a self-hosted store of personal agents that help run your day.
- [**Crew**](https://github.com/imsid/crew) - a self-hosted crew of role-based agents for analytics and product work, with a CLI and a web UI.

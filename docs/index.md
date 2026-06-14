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
It's designed around [Host-to-Agent Protocol (H2A)](rfcs/host-to-agent-protocol.md) that standardizes interactions between user applications and agents. Agents are model agnostic and come with powerful built-in tools, skills, memory, observability, and a API/CLI for access.

**Install:**

```bash
uv add mashpy
```

**Build Agent Pool:**

```python
## my_agent/spec.py

from mash.runtime import Host, HostBuilder

def build_pool():
  pool = (
      HostBuilder()
      .agent(ConciergeAgent(), metadata=...)
      .agent(ResearchAgent(), metadata=...)
      .host(Host(host_id="assistant", primary="concierge", subagents=("research",)))
      .build()
  )
  return pool
```

**Start the host:**

```bash
mash host serve --host-app my_agent.spec:build_pool --host 127.0.0.1 --port 8000
```

**Browse available agents:**
```bash
mash browse
```

**Compose an assistant host with agents:**
```bash
mash compose assistant --primary concierge --subagents research
```

**Talk to your agent with the Mash CLI:**
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

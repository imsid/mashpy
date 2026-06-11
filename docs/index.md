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

Install:

```bash
uv add mashpy
```

Compose Agents:

```python
## my_agent/spec.py

from mash.runtime import HostBuilder

def build_host():
  host = (
      HostBuilder()
      .primary(PrimaryAgent())
      .subagent(ResearchAgent(), metadata=...)
      .build()
  )
  return host
```

Start the host:

```bash
mash host serve --host-app my_agent.spec:build_host --host 127.0.0.1 --port 8000
```

Talk to your agent with the Mash CLI:

```bash
mash repl --api-base-url http://127.0.0.1:8000 --agent my-agent
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

- [**Building an agent CLI**](posts/building-agent-clis.md): custom CLI development
- [**Deploying a Mash Host**](posts/how-to-deploy.md): laptop, Docker, and cloud

## Applications

- [**Pilot CLI**](https://github.com/imsid/mash-pilot) - a multi-agent CLI guide to build and deploy Mash agents.

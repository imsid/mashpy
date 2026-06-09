---
title: Mash
description: A self-hosted runtime and SDK for building multi-agent applications.
hide:
  - navigation
  - toc
---

# Mash

**Build self-hosted, multi-agent applications in Python.**

Mash is a Python SDK and host runtime for composing a primary agent, specialized
subagents, and durable workflows behind a single host — with built-in tools,
skills, memory, observability, and a CLI/REPL.

```bash
pip install mashpy
```

```python
from mash.runtime import HostBuilder

host = (
    HostBuilder()
    .primary(PrimaryAgent())
    .subagent(ResearchAgent(), metadata=...)
    .build()
)
```

## Start here

- [**Product brief**](posts/product-brief.md) — what Mash offers and where it fits
- [**Deploying a Mash Host**](posts/how-to-deploy.md) — laptop, Docker, and cloud
- [**Building an agent CLI**](posts/building-agent-clis.md) — custom CLI development
- [**H2A Protocol RFC**](rfcs/host-to-agent-protocol.md) — the host-to-agent boundary

## Links

- [GitHub repository](https://github.com/imsid/mashpy)
- [Pilot example app](https://github.com/imsid/mash-pilot)

Use the search bar (top right) to find anything across the site.

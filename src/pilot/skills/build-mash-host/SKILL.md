---
name: build-mash-host
description: Compose multiple Mash agents into a host with a primary and subagents.
---

# Build a Mash Host

You are helping a developer compose several Mash agents into a multi-agent
application. The model to keep in mind:

| Concept | Role |
|---|---|
| `AgentPool` | The deployed flat pool of role-less agents — the unit of deploy |
| `Host` | A composition over the pool naming a primary and subagents — the unit of composition |
| `AgentMetadata` | Required self-description registered with every agent; the routing surface delegation decisions are made from |
| `HostBuilder` | Fluent builder producing an `AgentPool` from agents, workflows, and host definitions |

Agents never carry roles. "Primary" and "subagent" only exist inside a `Host`,
so the same agent can be primary in one host and a subagent in another. One
pool can serve many hosts at once.

This skill covers composition and routing. For scaffolding each individual
agent (tools, LLM provider, `AgentConfig`, env vars, serving), load
`build-mash-agent`; for durable step pipelines, load `build-mash-workflow`.

## Step 1: Register Agents into the Pool

Register every agent role-less into the flat pool, then compose a `Host` over
it:

```python
from mash.runtime import AgentMetadata, Host, HostBuilder

pool = (
    HostBuilder()
    .agent(AssistantAgent(), metadata=AgentMetadata(...))
    .agent(
        ResearchAgent(),
        metadata=AgentMetadata(
            display_name="Research Agent",
            description="Handles deep research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Delegate research-heavy questions here.",
        ),
    )
    .host(
        Host(
            host_id="assistant",
            primary="assistant",
            subagents=("research",),
        )
    )
    .build()
)
```

`AgentMetadata` is required for every pooled agent — registration rejects an
agent without it. Write it like routing documentation, not marketing copy:
the primary's model reads it to decide when to delegate.

A `Host` can also attach workflows (`Host(..., workflows=("my-pipeline",))`);
see `build-mash-workflow` for defining them.

## How Host Routing Works

Submitting a request to a host routes it to that host's primary and — for
that request only — wires the primary with an `InvokeSubagent` tool plus a
directory of the host's subagents, built from their `AgentMetadata`. The
primary's model reads that directory to decide when to delegate, so
**delegation quality is a prompt-engineering surface**: vague
`usage_guidance` produces vague routing. A bare request to the same agent
(`POST /v1/agent/{agent_id}/request`) gets no directory and no delegation
tool — the agent answers alone.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/hosts/assistant/request \
  -H "Content-Type: application/json" \
  -d '{"message": "find recent papers", "session_id": "s-1"}'
# -> {"request_id": "...", "agent_id": "assistant", "session_id": "s-1"}
```

The response names the primary `agent_id`; stream results from the existing
`GET /v1/agent/{agent_id}/request/{request_id}/events`. Each request
snapshots the host composition at submit time, so redefining a host never
affects in-flight requests.

## Code-Defined vs Dynamic Hosts

A host is just data (a few agent ids), so there are two ways to define one:

- **Code-defined** (`.host(Host(...))` as above) — ships with the deploy and
  is re-created on every restart. Use this for compositions that must always
  exist, e.g. when other clients target the deployment.
- **Dynamic** — defined on a running pool, in code with
  `pool.define_host(Host(host_id="research-only", primary="research"))` or
  over the API with an idempotent `PUT`:

  ```bash
  curl -X PUT http://127.0.0.1:8000/api/v1/hosts/research-only \
    -H "Content-Type: application/json" \
    -d '{"primary": "research", "subagents": [], "workflows": []}'
  ```

  Dynamic hosts are in-memory: they disappear on restart and must be
  re-`PUT` (the PUT is idempotent, so clients can safely define their
  composition on every startup). If a composition references an agent that
  isn't in the pool, the server rejects it with a clear error.

Because hosts are cheap, a client can compose a host per task, route a few
requests through it, and forget it.

## Composing from the CLI

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret
mash agents                                          # what's in the pool
mash compose --host assistant --primary assistant --subagents research
mash repl                                            # routed through 'assistant'
```

`mash compose` issues the idempotent `PUT /v1/hosts/{host_id}` and pins later
commands to that host; `mash hosts` lists defined compositions. The REPL
target is fixed for its lifetime — exit and `mash compose` again to change
composition. With a code-defined host (`.host(...)` in `build_pool()`), skip
`mash compose` and `mash repl` against the shipped host directly.

## Reference Documentation

- Runtime & hosting: `src/mash/runtime/README.md`
- How composition works under the hood (delegation, per-request role wiring,
  mirrored traces): `docs/posts/composing-agents.md`
- Driving composition purely over HTTP: `docs/posts/building-dynamic-hosts-apis.md`
- API server: `src/mash/api/README.md`
- CLI: `src/mash/cli/README.md`

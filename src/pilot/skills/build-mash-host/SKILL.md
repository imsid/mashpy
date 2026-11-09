---
name: build-mash-host
description: Compose several Mash agents and workflows into a host that the companion routes.
---

# Build a Mash Host

You are helping a developer compose several Mash agents into a multi-agent
application. The model to keep in mind:

| Concept | Role |
|---|---|
| `Pool` | The deployed flat pool of agents — the unit of deploy |
| `Host` | A set of agents and workflows from the pool — the unit of composition |
| `companion` | The built-in agent every pool has; it takes every host request and routes it |
| `AgentMetadata` | Required self-description registered with every agent; the companion routes by it |
| `HostBuilder` | Fluent builder producing a `Pool` from agents, workflows, and host definitions |

Every agent in a host is the same kind of member. The companion decides, per
user message, which of them run and how many, or which workflow runs, waits
for them and writes the answer. One pool can serve many hosts at once, and an
agent can be in any number of them.

This skill covers composition and routing. For scaffolding each individual
agent (tools, LLM provider, `AgentConfig`, env vars, serving), load
`build-mash-agent`; for durable step pipelines, load `build-mash-workflow`.

## Step 1: Register Agents into the Pool

Register every agent into the flat pool, then compose a `Host` over it:

```python
from mash import AgentMetadata, Host, HostBuilder

pool = (
    HostBuilder()
    .agent(BillingAgent(), metadata=AgentMetadata(...))
    .agent(
        ResearchAgent(),
        metadata=AgentMetadata(
            display_name="Research Agent",
            description="Handles deep research queries.",
            capabilities=["web search", "document analysis"],
            usage_guidance="Research-heavy questions.",
        ),
    )
    .host(Host(host_id="assistant", agents=("billing", "research")))
    .build()
)
```

`AgentMetadata` is required for every pooled agent — registration rejects an
agent without it. Write it like routing documentation, not marketing copy:
the companion's model reads it to decide which agent gets a message.

A host never lists `companion`. A `Host` can also attach workflows
(`Host(..., workflows=("my-pipeline",))`), which the companion runs when a
message asks for exactly what one does; give each workflow
`metadata={"display_name": ..., "description": ...}` so the companion can
choose it. See `build-mash-workflow` for defining them.

The companion needs a model: it picks a provider from `GEMINI_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `OSS_BASE_URL`, and
`COMPANION_AGENT_<PROVIDER>_MODEL` overrides the model.

## How Host Routing Works

Submitting a request to a host runs it on the companion and, for that request
only, gives it a directory of the host's agents built from their
`AgentMetadata`. The companion hands work to them with `RunSubAgents`, which
runs one or more child requests at the same time, each in a session of its
own; a follow-up goes back to the session that answered. Only the companion
routes: the host's agents never get `RunSubAgents`. Routing quality is a
prompt-engineering surface: vague `usage_guidance` produces vague routing. A
request sent to one agent directly (`POST /v1/agent/{agent_id}/request`)
bypasses the companion.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/hosts/assistant/request \
  -H "Content-Type: application/json" \
  -d '{"message": "find recent papers", "session_id": "s-1", "user_id": "u-1"}'
# -> {"request_id": "...", "agent_id": "companion", "session_id": "s-1", "user_id": "u-1"}
```

Stream results from `GET /v1/agent/companion/request/{request_id}/events`.
Each request snapshots the host composition at submit time, so redefining a
host never affects in-flight requests.

## Code-Defined vs Dynamic Hosts

A host is just data (a few agent and workflow ids), so there are two ways to
define one:

- **Code-defined** (`.host(Host(...))` as above) — ships with the deploy and
  is re-created on every restart. Use this for compositions that must always
  exist, e.g. when other clients target the deployment.
- **Dynamic** — defined on a running pool, in code with
  `pool.define_host(Host(host_id="research-only", agents=("research",)))` or
  over the API with an idempotent `PUT`:

  ```bash
  curl -X PUT http://127.0.0.1:8000/api/v1/hosts/research-only \
    -H "Content-Type: application/json" \
    -d '{"agents": ["research"], "workflows": []}'
  ```

  Dynamic hosts are in-memory: they disappear on restart and must be
  re-`PUT` (the PUT is idempotent, so clients can safely define their
  composition on every startup). If a composition references an agent that
  isn't in the pool, the server rejects it with a clear error.

Because hosts are cheap, a client can compose a host per task, route a few
requests through it, and forget it.

## Composing from the CLI

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --user-id u-1
mash agents                                          # what's in the pool
mash compose --host assistant --agents billing,research
mash repl                                            # routed through 'assistant'
```

`mash compose` issues the idempotent `PUT /v1/hosts/{host_id}` and pins later
commands to that host; `mash hosts` lists defined compositions. The REPL
target is fixed for its lifetime — exit and `mash compose` again to change
composition. With a code-defined host (`.host(...)` in `build_pool()`), skip
`mash compose` and `mash repl` against the shipped host directly.

## Reference Documentation

- Runtime & hosting: `src/mash/runtime/README.md`
- How composition works under the hood (routing, child sessions, traces):
  `docs/posts/composing-agents.md`
- Driving composition purely over HTTP: `docs/posts/building-dynamic-hosts-apis.md`
- API server: `src/mash/api/README.md`
- CLI: `src/mash/cli/README.md`

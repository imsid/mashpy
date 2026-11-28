---
title: Composing Agents Under One Host
description: How a flat pool of agents gets composed into hosts, how the built-in companion routes each host request with RunSubAgents, and how traces and infrastructure stay shared.
date: 2026-06-10
author: imsid
tags:
  - internals
  - multi-agent
---

# Composing Agents Under One Host

A Mash deployment is a flat pool of agents plus the hosts composed over them:

```python
pool = (
    HostBuilder()
    .agent(PilotSpec(), metadata=AgentMetadata(...))
    .agent(
        CliCopilot(),
        metadata=AgentMetadata(
            display_name="CLI Copilot",
            description="Answers questions about the mash CLI.",
            capabilities=["cli commands", "repl usage"],
            usage_guidance="Use for CLI-specific questions.",
        ),
    )
    .agent(ApiCopilot(), metadata=AgentMetadata(...))
    .host(Host(host_id="guide", agents=("pilot", "cli-copilot", "api-copilot")))
    .build()
)
```

Each spec becomes a full `AgentRuntime` with its own model, tools, config, and memory scope, everything this series has covered. When the host starts, each one runs in its own process, a subprocess of the host. The builder also registers one agent the app never writes: `companion`, the built-in router. The pool is the unit of deploy, and a `Host` is the unit of composition.

## Hosts are data

A `Host` names a set of agents and, optionally, workflows, all by id. Every member is the same kind of member. A `Host` is an immutable value, defined in code at build time like above, or on a running pool over the control API:

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/hosts/cli-help \
  -d '{"agents": ["cli-copilot"], "workflows": []}'
```

An event posted to a host (`POST /v1/sessions/{session_id}/events` with its `host_id`) runs as a request on the companion, which snapshots `{host_id, agents, workflows}` when the event starts. The snapshot rides in the request's metadata on its `invoke_agent.start` event, so a crashed request recovers with the composition it started with, even if someone re-`PUT` the host mid-flight. Redefining a host only affects events that start later.

An agent can be in any number of hosts at once. Hosts are a few strings, so they're cheap to create: a client application can compose a host per task, post a few events to it, and forget it.

## Metadata and the agent directory

`AgentMetadata` is required at registration; `register_agent` rejects an agent without it. The companion routes by it. When a request carries a host snapshot, the per-turn agent build appends an `AGENTS` directory to the companion's system prompt: each of the host's agents with its display name, description, capabilities and usage guidance. The companion's model reads that directory to decide which agents a message belongs to.

Routing quality is therefore a prompt-engineering surface. Vague `usage_guidance` produces vague routing.

The directory is per request. A workflow's agent step runs on its agent directly, without the companion, and that agent gets neither the directory nor a routing tool.

## RunSubAgents

Alongside the directory, the per-turn build installs one tool on the companion, and on no other agent:

```python
# src/mash/tools/subagent.py
class RunSubAgentsTool:
    name = "RunSubAgents"
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", ...},
                        "prompt": {"type": "string"},
                        "session_id": {"type": "string", ...},
                        "output_schema": {"type": "object", ...},
                    },
                    "required": ["agent_id", "prompt"],
                },
            },
            "timeout_seconds": {"type": "integer", ...},  # defaults to AgentConfig.subagent_timeout_seconds (3600), at most that
        },
        "required": ["requests"],
    }
```

Each item runs as its own child request on one of the host's agents, and all of them run at the same time. One item hands a message to one agent. Several items split a request whose parts don't depend on each other, to different agents or to the same agent more than once. The tool returns when every child has ended, with one entry per item in order: the child's request id, the session it ran in, its trace id, its status, and its response or error. The user talks only to the companion. When a child stops to ask for an approval or a question, `RunSubAgents` opens the same interaction on the companion's request, marked with the child it came from, and passes the user's answer back to the child. The child's entry lists what it asked and what the user said.

Each agent runs in its own process, so starting a child is an HTTP call. The companion's process submits a normal Mash request to the host's `/h2a` relay with its agent token, a `parent` naming the calling request, and the request's host snapshot. The relay refuses an agent id that isn't in the `agents` of the snapshot stored with the parent request, even if it exists in the pool, and forwards the rest to that agent's process. The child request goes down the same `submit_request` path and durable loop as the companion's own request, and its response streams back through the relay to become the tool result the companion observes on its next think.

Each child runs in a session of its own. Without a `session_id` in its item, the child starts a new session named after its request id, so a replayed call lands in the same one. With the `session_id` of an earlier entry, it continues that session and replays its own earlier briefs and answers. Two runs of one agent in two sessions never see each other's work, and the companion's conversation never replays a child's turns, since memory is keyed by session and `agent_id`. The relay lets a child continue only a session of the same agent that was started from the companion's session.

The companion keeps track of which session produced which answer in its routes. When its turn is saved, every `RunSubAgents` entry of the request becomes a route in the turn's metadata: the agent, its session, its trace and the status. The companion replays its last 10 turns as text, and puts each reply's routes at the top of the user message that follows it, for the model only, so a follow-up such as "why did you use feedparser?" goes back to the session the routes list for that reply. They stay out of the reply itself, where the model would copy them into new answers. What the agent was asked and answered stays in the event log; the companion reads it with its log tools when it needs to.

After reading the results, the companion picks how to answer. When one agent's answer answers the message as asked, it calls `ForwardAnswer` with that entry's request id, and the user gets the answer unchanged with no further model call. When the answer misses, it runs the same session again with a corrected brief. When several agents answered, or the results need combining, it writes its own reply.

The child's events stay in its own log, under its own request id. The companion's `execute_tool.start` for `RunSubAgents` names each child's agent and request id, so a client streaming the companion's request can open each child's stream and watch the routing happen live, which is what the REPL does. The [trace projection](reading-a-trace.md) uses the same ids to nest each child's `invoke_agent` span under the call that started it, so the admin UI, trace analysis and an OTLP export all show the request as one trace.

```mermaid
flowchart TD
    H["host: guide\n(agent and workflow ids, /h2a relay)"]
    C["process: companion\n(directory + RunSubAgents, per request)"]
    A["process: cli-copilot\nown model, memory scope"]
    B["process: api-copilot"]
    W["workflow-only agents\n(not in hosts)"]
    H -- "request + snapshot" --> C
    C -- "child request" --> H
    H -- "checked against snapshot" --> A
    H -- "checked against snapshot" --> B
    PG[("Postgres\nevents, sessions, event logs")]
    H --- PG
    C --- PG
    A --- PG
```

`RunSubAgents` runs as an ordinary step. Each child request is submitted under an id derived from the companion's request id, the call's position and the item's index: a UUID5 of `{request_id}-sub-{loop}-{call}-{index}`. A replayed or retried step resubmits those ids, and each child's runtime returns the request it already started instead of starting a second. Cancelling the event cancels the companion's request and its running children.

When the host lists workflows, the companion also gets a `WORKFLOWS` list in its context and a `RunWorkflow` tool, which starts a run under the request's app and policy through the same relay and waits for its result.

## The second registration kind

Besides pooled agents, deployments can carry **workflow-only agents**, specs registered through `HostBuilder.workflow(...)` (or `register_workflow_agent`) that exist to execute workflow tasks. They're full runtimes, but they're hidden from public agent listings and can't be named in a host, so the companion can't route to them and clients can't address them. They surface in the next post.

The bundled eval agent uses the normal registration kind instead. It appears in the agent catalog so the spec behind its workflow steps is inspectable, and every pool includes it plus Masher's trace-digest, eval-curation, eval-generation, and scoring workflows.

## Shared infrastructure

The pool owns the stores, as [the runtime store post](persistence-store.md) covered: every agent using the default `build_memory_store()` shares one pool, one LISTEN connection, and one memory connection, regardless of agent count.

Sharing stops at infrastructure. Memory reads and writes are scoped by `agent_id`, and runtime events carry it too, so two agents in one pool stay as isolated as two agents in separate processes. One consequence worth knowing: memory is keyed by agent id, not by host, so an agent keeps its memory whether it's serving one host or three. If you need isolated instances of the same spec, register it twice under different ids.

Routing covers work that arrives as conversation. Scheduled, repeatable pipelines get their own layer, built from the same parts, and that's the next post.

*Next: [Workflows as Step Pipelines](workflows-as-step-pipelines.md).*

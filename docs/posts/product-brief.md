---
title: Mash Product Brief
description: Mash is a self-hosted, durable runtime for code-authored worklows with agents behind one API.
date: 2026-07-25
author: imsid
tags:
  - product
  - overview
---

# Mash Product Brief

Mash is a self-hosted, durable runtime for code-authored automations. An
automation is a workflow: an ordered pipeline of typed steps, where a step is
deterministic Python or one run of a harnessed agent. Control flow stays in
code. Agents and workflows deploy together behind one API, on your own
Postgres.

You author two things. A workflow is the pipeline itself, the shape of the
automation as code. An agent is an LLM loop running inside a harness that gives
it tools, skills, memory, a sandbox for the code it writes, a browser for the
sites it works on, and a durable request lifecycle. An agent serves user requests directly, runs as a step
inside a workflow, or both. Either way, it works for a user.

## Workflows

A workflow is an ordered pipeline of typed steps. Each step declares a pydantic
input and output, each step's output threads into the next step's input, and
the last step's output is the run result. A `CodeStep` is a deterministic
Python function. An `AgentStep` is one run of an agent's loop with a schema on
both edges: typed input in, structured output out, validated against the
step's declared schema.

The pipeline is code you can read, diff, test, and replay. Dynamic work such as
fan-out over rows or branching happens inside a step body; the model reasons
inside its step and never owns the pipeline. Runs are durable. Each step body
executes as a memoized durable step, a failed run resumes from the failed step
under the same run id, and a per-step audit trail in Postgres records every
step's status, inputs, outputs, and attempts.

## The agent harness

Each agent is an `AgentSpec`: a Python contract that names the agent, picks its
LLM, and declares its system prompt, tools, and skills. The loop runs inside a
durable request: think, act, observe, with tools executing in the agent's
process, the code the model writes running in the user's sandbox, skills loaded
on demand, memory management, and structured output. Every
request is recorded as replayable runtime events, so retries, restarts, and
long-running work recover mid-loop instead of starting over. An agent can pause
mid-request for a human approval or a question, and the pause survives a host
restart.

The [Host-to-Agent Protocol (H2A)](../rfcs/host-to-agent-protocol.md) is the
contract underneath: it standardizes how a request is submitted, how its
lifecycle streams back, how an agent pauses for a human, and how a request
recovers from failure. Every agent behind a Mash deployment speaks it, so the
application integrates once.

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
workflow step ──► │      │ ▲                      output    │
(api/cli)         │      │ │ user interaction               │
                  │      │ ▼ (approval / ask-user /         │
                  │      │    browser sign-in)              │
                  │      │                                  │
                  │      │  resumable · replayable          │
                  └──────┼──────────────────────────────────┘
                         ├────────────────────────────┐
                         │ run_code                   │ browser_*
                         ▼                            ▼
                  ┌──────────────────────────┐ ┌──────────────────────────┐
                  │  Sandbox: one per user   │ │  Browser: one per user   │
                  │  workspaces/<agent_id>   │ │  Chromium with a profile │
                  │  files persist           │ │  listed sites only       │
                  │  no secrets · no db      │ │  sign-in via live view   │
                  └──────────────────────────┘ └──────────────────────────┘
```

A request from a user and a step in a workflow ride the same durable loop, and
the code either one runs lands in the sandbox of the user it acts for. An agent
that lists sites drives that user's browser.

## Sandboxes

With a computer of its own, an agent can analyse a file, build a chart or a
page, keep a SQLite database, run the tests of the code it produced, and keep
the results for a later request. Mash runs that code in a sandbox, a container
for the user the request acts for.

Every request names its user with a `user_id`, which the application sends and
the host trusts. The host starts a user's sandbox on their first command and
stops it after 15 idle minutes. With Docker on the host, every agent gets
`run_code`, which runs Python, JavaScript or shell in the request user's
sandbox, in a folder of its own (`/home/agent/workspaces/<agent_id>`). An
agent whose spec names folders on its machine also gets `sandbox_put` and
`sandbox_get` to copy files in and out of those folders. Its files persist
across requests and restarts, and the user opens the pages and files it makes
at links the host serves. Code runs as an unprivileged `agent` user with a
clean environment. A sandbox holds only that user's files: the agents' secrets
and the database stay with the host.

Each agent runs as a process of the host, with its credentials, its model
connection, and the tools you registered. Those tools run in that process,
and the code the model writes goes to the sandbox.

## Browsers

Some work only happens in a website: downloading a bill from a utility's
portal, checking an order's status, filling in a form. Mash gives each user a
browser for it, a Chromium in a container of its own next to the user's
sandbox. Its profile keeps the sites the user signed in to, and every agent
acting for that user shares it.

An agent gets the browser tools when its spec lists sites with
`browser_sites()`, such as `https://*.example.com`. The tools are Playwright
MCP's page tools: the model reads the page as an accessibility tree, clicks,
types, and takes screenshots it can see. A URL outside the listed sites fails,
and a click that leaves them goes back without showing the page. Downloads
can be copied into the sandbox, where the agent's code reads them.

When a site needs a sign-in, the agent calls `browser_handoff`. The request
pauses on a durable interaction whose payload carries a live-view link to the
browser. The user opens it, signs in, and answers `done`; the REPL opens the
link and waits for Enter, and an app shows it and posts the answer. Mash never
sees the password, and the profile keeps the session for later requests. Code
in the sandbox has no route to the browser, so the user's signed-in sessions
stay out of reach of the code the model writes.

## The pool and hosts

The pool is the unit of deploy. `HostBuilder` composes agents and workflows
into a `Pool`, and the API server runs it. Agents in the pool are role-less; a
pool with only workflows is a valid deploy that serves workflow runs alone.

A host is a composition over the pool: a `host_id`, a primary agent, and a set
of subagents and workflows. Submitting a request to a host wires the primary
with delegation to that host's subagents, for that request only. Hosts are
defined in code at build time or dynamically over the API, so the same pool
serves different compositions without a redeploy.

```mermaid
flowchart TD
    U["User application<br/>(web · api · cli · app)"] --> API["Mash API server"]
    API --> HO["Host 'assistant'<br/>primary + subagents"]
    API --> WF["Workflow runs"]
    HO --> P["Pool"]
    WF --> P
    P --> A1["Agent: concierge"]
    P --> A2["Agent: research"]
    P --> W1["Workflow: research-brief"]
    A1 -- "run_code" --> S["Sandboxes<br/>one per user"]
    A2 -- "run_code" --> S
    A1 -- "browser_*" --> B["Browsers<br/>one per user"]
```

## The execution surface

The runtime exposes a structured HTTP API with SSE streaming, a CLI, and an
interactive REPL. The application tier is language-agnostic: a React frontend,
a Go service, a mobile app, a cron job, or a terminal drives a deployment over
plain HTTP. The agent is written once, in Python, behind the API; nothing that
consumes it needs to share its stack.

## One harness across frontier and open-source models

Open-source models like Gemma, Qwen, and DeepSeek now sit near the top of
public benchmarks for reasoning, coding, and tool use, within range of the
frontier models on many tasks. Running them is cheap: a hosted gateway charges
a fraction of frontier API pricing, and self-hosting on your own hardware
removes per-token cost entirely.

Mash runs these models on the same durable harness as the frontier providers.
An agent that uses Anthropic, OpenAI, or Gemini moves to an open-source model
served by vLLM, Ollama, or OpenRouter by swapping one provider line. The tool
loop, human-in-the-loop pauses, workflows, observability, and durability stay
the same across every model.

Because each agent picks its own model, one deployment can mix them. A
high-volume triage or extraction agent runs on a local open-source model while
the agent that handles the hard reasoning runs on a frontier model, in the same
deployment, behind the same API. You match the model to the task and the budget,
and the harness underneath is identical.

## Traces

The runtime events a request writes are named after OpenTelemetry spans, so
every request already reads as a trace in the GenAI conventions: an
`invoke_agent` span for the run, a `chat` span for each model call with its
token usage, an `execute_tool` span for each tool call, and subagent requests
nested under the call that started them. The admin UI, the REPL's `/trace`
and the trace analysis API all read that projection of the event log, with no
second store to keep in sync.

The same spans go to Grafana, Datadog, Honeycomb or any OTLP backend when you
install `mashpy[otel]` and set `OTEL_EXPORTER_OTLP_ENDPOINT` on the host. A
request is exported when it ends, with its log records and the GenAI client
metrics, and a workflow run goes out as one trace with each agent step's
request under its step. A W3C `traceparent` on submit puts the request inside
your own service's trace. Prompts, completions and tool I/O stay out of the
export unless you turn content capture on.

## Evals

The host is also the unit of evaluation. A user request lands on the
composition: the primary agent, the delegation choices it makes, the tools and
subagents behind it. Mash evals run against the host rather than any single
agent, so what gets measured is the path the application actually takes and
the response the user actually receives.

Synthetic evals ship in the SDK and runtime, turned on per pool with
`HostBuilder.enable_evals()`. A built-in workflow reads the
host's declared capabilities and the developer's guidance, then generates a
dataset of test scenarios and a weighted scoring rubric before the first user
ever sends a message. Each scoring run is an experiment: it snapshots the live
composition and agent specs, runs every dataset row through the host, and
records the results.

Every experiment measures two kinds of signal. Deterministic quantitative
metrics come from each row's runtime events: latency, tokens, steps, tool
calls, per-subagent breakdowns. Qualitative criteria are non-deterministic and
scored by the built-in eval agent against the rubric: task completion,
subagent coordination, response quality, each with a rationale. Comparing two
experiments answers three questions in one view: what changed in the agent
specs, how quality moved per criterion and per row, and what the change costs
in tokens and latency. See [Synthetic evals](synthetic-evals.md) for the full
design.

## Where to go next

- [**Code as the Source of Truth**](code-as-the-source-of-truth.md): one
  automation taken from a pure-code pipeline to an agent step to a deployment
- [**Getting started**](../index.md): install, define agents and workflows,
  and run your first host
- [**Mash Under the Hood**](mash-under-the-hood.md): what Mash provides, one
  host over many agents, the durable harness, observability, and the
  self-hosted interfaces
- [**Synthetic Evals**](synthetic-evals.md): generated datasets and rubrics,
  experiments over the live host, read-time comparison
- [**H2A Protocol RFC**](../rfcs/host-to-agent-protocol.md): the full
  protocol specification
- [**Building an agent CLI**](building-agent-clis.md): custom CLI development
  with dynamic host composition

---
title: Exploring Mash with Pilot
description: Install the Pilot CLI, connect to the hosted Pilot agent, and ask it questions about the Mash codebase.
date: 2026-06-11
author: imsid
tags:
  - guide
  - pilot
---

# Exploring Mash with Pilot

[Pilot](https://github.com/imsid/mash-pilot) is a multi-agent codebase guide for mashpy, built with Mash itself. You ask it questions about the codebase in a terminal REPL, and a primary agent routes each one to the copilot that owns the relevant module, then synthesizes the answer. It's also the reference application behind this documentation, so the delegation, workflows, dynamic skills, and traces from the internals series are all observable in one running system.

## Install and connect

```bash
curl -fsSL https://raw.githubusercontent.com/imsid/mash-pilot/main/install.sh | sh
pilot repl
```

## Asking questions

Plain input goes to the agent. Questions about the Mash codebase are what Pilot is tuned for:

```text
> Summarize how HostBuilder wires the primary agent, subagents, and workflows.
> Trace how an accepted request moves through AgentRuntime, RuntimeStore, and RequestEngine.
> Compare src/mash/runtime and src/mash/workflows responsibilities.
```

Behind the prompt is the [composition](composing-agents.md) from the internals series: a primary agent plus five copilots, each scoped to one package.

| Agent | Scope |
|-------|-------|
| `pilot` (primary) | shared and cross-cutting: `core`, `tools`, `skills`, `logging`, `memory` |
| `cli-copilot` | `src/mash/cli`: commands, REPL, terminal rendering |
| `api-copilot` | `src/mash/api`: HTTP routes, FastAPI, telemetry UI |
| `mcp-copilot` | `src/mash/mcp`: MCP client/server, transport, tool adaptation |
| `runtime-copilot` | `src/mash/runtime`: request lifecycle, event sourcing, durability |
| `workflow-copilot` | `src/mash/workflows`: DBOS orchestration, task state, run status |

The primary delegates based on the question and synthesizes across copilots when a question spans modules. The routing is visible as it happens, because the shell renders subagent trace frames live: ask about request durability and you can watch the question get handed to `runtime-copilot`. To skip the routing and talk to one specialist directly, switch with `/use cli-copilot`.

## Scaffolding your own agent

Pilot carries a `build-mash-agent` skill, so it can go from answering questions about Mash to generating a Mash application from a description:

```text
> Build me a customer support agent with a knowledge base search tool and human approval for refunds.
> Scaffold a multi-agent code reviewer with separate agents for security, style, and correctness.
> I need an agent that connects to my MCP server at localhost:3000 and uses Gemini as the LLM.
```

## Two commands worth trying

Each of Pilot's custom commands exists to demonstrate a Mash feature in use.

`/changelog [N]` generates a changelog from the last N mashpy commits (default 5). The command registers its skill and workflow definition on the host at the moment it runs, which is the [dynamic publishing](workflows-and-task-state.md) flow exercised end to end: the workflow definition stays a thin pointer and the instructions travel as skill markdown.

`/quiz` starts an interactive quiz about Mash internals: three questions of increasing difficulty, with follow-up questions welcome at any point. It runs on a dedicated [workflow-only agent](composing-agents.md) registered at startup, composed alongside the primary and its copilots.

## Watching it run

After any answer, `/trace` shows where the time went: the timing breakdown, per-tool stats, and slowest operations from [the trace post](reading-a-trace.md). For a delegated question, the subagent call appears as its own span, so you can see how much of the answer's latency belonged to the copilot.

The host also serves the telemetry UI with the span waterfall, at `/telemetry` on whichever deployment you're connected to ([hosted](https://pilot-tk3b.onrender.com/telemetry), or `http://127.0.0.1:8000/telemetry` locally).

## REPL reference

| Command | What it does |
|---------|--------------|
| `/agents` | list pilot and its copilots |
| `/use <agent_id>` | talk to one copilot directly |
| `/history [N]` | recent turns in this session |
| `/trace [N]` | latency analysis for recent traces |
| `/workflow list\|run\|status` | inspect or run registered workflows |
| `/changelog [N]` | changelog from the last N mashpy commits |
| `/quiz` | interactive quiz about Mash internals |
| `/status`, `/session`, `/sessions` | connection and session info |
| `/help`, `/clear`, `/exit` | shell basics |

Pilot's CLI is about a hundred lines on top of `MashRemoteShell`: argument parsing, agent resolution, and the two custom command registrations. [Building an Agent CLI](building-agent-clis.md) walks through building the same thing for your own agent.

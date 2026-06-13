---
title: Exploring Mash with Pilot
description: Run Pilot, the self-hosted app store for agents built on Mash — browse the catalog, compose agent teams into hosts, and enter them from the CLI.
date: 2026-06-12
author: imsid
tags:
  - guide
  - pilot
---

# Exploring Mash with Pilot

[Pilot](https://github.com/imsid/mash-pilot) is the reference user
application for the seam the [product brief](product-brief.md) describes: a
self-hosted **app store for agents**, built with Mash itself. The repo is a
catalog of agents, a deployment is your store, host compositions are your
installed apps, and a terminal CLI is the storefront. It's also the
application behind this documentation series, so the delegation, dynamic
composition, workflows, skills, and traces from the internals posts are all
observable in one running system.

## Start your store

A Pilot deployment is one container (Postgres embedded) plus the CLI:

```bash
docker run -d --name pilot -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v pilot-data:/var/lib/pilot \
  ghcr.io/imsid/mash-pilot:latest

curl -fsSL https://raw.githubusercontent.com/imsid/mash-pilot/main/install.sh | sh
pilot browse
```

(Add `-e GITHUB_MCP_PAT=...` for the `pilot` guide's commit-inspection
tools; set `MASH_DATABASE_URL` to bring your own Postgres instead of the
embedded one.)

`pilot browse` renders the catalog: seven pooled agents, each listed through
the `AgentMetadata` it registered with — the same metadata that becomes the
delegation directory when an agent serves as a subagent, as
[the composition post](composing-agents.md) covered. The store runs wherever
you run the container: laptop, homelab, your own server.

## Compose a team

The pool is flat — the deployment ships no host compositions. Which agents
work together is your configuration:

```bash
pilot compose my-guide --primary pilot --subagents cli-copilot,api-copilot
pilot repl --host my-guide
```

Compositions live in the CLI's host config file (`~/.pilot/hosts.json`),
which ships with the `guide` composition as its default entry and is
what `pilot hosts` lists. The deployment side is [dynamic host
definition](building-dynamic-hosts-apis.md): a host is a few strings,
validated synchronously, held in server memory — so the CLI publishes your
config with idempotent `PUT`s every time it enters a REPL, and deployment
restarts don't matter.

Inside the REPL everything is scoped to the host you entered: plain messages
route to its primary, delegation is limited to its subagents, and `/agents`
shows exactly the team you composed. To change the team, exit and re-run
`pilot compose` (define-or-replace), or switch with
`pilot repl --host <other>`.

## A store for Mash

Pilot's catalog is about Mash itself. Personal agents live in
[mash-pa](https://github.com/imsid/mash-pa), a sibling store with its own
`pa` CLI: a `morning-brief` over the GitHub MCP server and a `finance-watch`
over a local ledger, the integration space the product brief sketches. Both
stores are the same Mash machinery pointed at different catalogs, which is
the point of a flat pool and host compositions as configuration.

## The featured app: the guide

`pilot repl --host guide` opens the Mash codebase guide — the composition
from [the internals series](composing-agents.md), shipped as the default
entry in the host config file:

| Agent | Scope |
|-------|-------|
| `pilot` (primary) | shared and cross-cutting: `core`, `tools`, `skills`, `logging`, `memory` |
| `cli-copilot` | `src/mash/cli`: commands, REPL, terminal rendering |
| `api-copilot` | `src/mash/api`: HTTP routes, FastAPI, telemetry UI |
| `mcp-copilot` | `src/mash/mcp`: MCP client/server, transport, tool adaptation |
| `runtime-copilot` | `src/mash/runtime`: request lifecycle, event sourcing, durability |
| `workflow-copilot` | `src/mash/workflows`: DBOS orchestration, task state, run status |

```text
> Summarize how HostBuilder composes the agent pool, hosts, and workflows.
> Trace how an accepted request moves through AgentRuntime, RuntimeStore, and RequestEngine.
> Compare src/mash/runtime and src/mash/workflows responsibilities.
```

The primary delegates by reading the copilots' metadata and synthesizes
across them when a question spans modules; the shell renders the subagent
trace frames live, so you watch the routing happen. It also carries a
`build-mash-agent` skill, so it can scaffold a new Mash application from a
description.

## Two commands worth trying

`/changelog [N]` generates a changelog from the last N mashpy commits
(available in sessions targeting the `pilot` primary, which is the agent
that carries the repo workspace). The
command registers its skill and workflow definition on the host at the
moment it runs — the [dynamic publishing](workflows-and-task-state.md) flow
exercised end to end.

`/quiz` starts an interactive quiz about Mash internals, executed by the
pooled `quiz-me` agent through the `pilot-quiz`
[workflow](workflows-and-task-state.md). Workflows are attached to hosts in
your config (`guide` attaches `pilot-quiz` by default), and `/quiz`
only exists in REPLs of hosts that attach it — compose it onto your own
host with `--workflows pilot-quiz`.

## Watching it run

After any answer, `/trace` shows where the time went: the timing breakdown,
per-tool stats, and slowest operations from [the trace
post](reading-a-trace.md). For a delegated question the subagent call is its
own span, so you can see how much latency belonged to the copilot. The host
also serves the telemetry UI with the span waterfall at
`http://127.0.0.1:8000/telemetry`.

## Reference

| CLI command | What it does |
|---|---|
| `pilot browse` | catalog listings and the hosts composed on the deployment |
| `pilot compose <id> --primary <agent> [--subagents a,b]` | define-or-replace a composition |
| `pilot hosts` | your saved compositions, with live status |
| `pilot repl --host <id>` | enter a host's REPL, scoped to its team (`--agent <id>` for one bare agent) |
| `pilot serve` | run the store from a source install |

| REPL command | What it does |
|---|---|
| `/agents`, `/hosts` | list agents and compositions |
| `/history [N]`, `/trace [N]` | recent turns; latency analysis |
| `/workflow list\|run\|status` | inspect or run the workflows attached to this host |
| `/changelog [N]`, `/quiz` | the two feature demos |
| `/status`, `/session`, `/sessions` | connection and session info |
| `/help`, `/clear`, `/exit` | shell basics |

Pilot's CLI is a thin layer over `MashRemoteShell` and `MashHostClient`:
argument parsing, the storefront commands, and local persistence for
compositions. [Building an Agent CLI](building-agent-clis.md) walks through
building the same thing for your own agent.

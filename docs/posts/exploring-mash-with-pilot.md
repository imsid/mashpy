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

Pilot is the reference user application for the seam the [product brief](product-brief.md)
describes: a self-hosted **app store for agents**, built with Mash itself. It lives in the
mashpy repo at [`src/pilot/`](https://github.com/imsid/mashpy/tree/main/src/pilot/) — the
catalog of agents, a deployment is your store, host compositions are your
installed apps, and a terminal CLI is the storefront. It's also the
application behind this documentation series, so the delegation, dynamic
composition, workflows, skills, and traces from the internals posts are all
observable in one running system.

## Start your store

A Pilot deployment is Postgres and a Mash host, run with Docker Compose from a
checkout of the repo. The host runs each agent as a process inside its
container. Through the Docker socket it starts a sandbox per user, where the
agents' code runs, and mounts the checkout read-only into each one at
`/home/agent/mashpy`. Put `GEMINI_API_KEY`, `OPENROUTER_API_KEY` and
`MASH_HOST_SECRET` (`openssl rand -hex 32`) in `.env`, then:

```bash
git clone https://github.com/imsid/mashpy.git && cd mashpy
docker compose -f docker-compose.pilot.yml up -d --build

curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
mash connect --api-base-url http://127.0.0.1:8000 --user-id me
pilot browse
```

`pilot browse` renders the catalog: the `pilot` guide, the `builder`, and
masher's eval agents, each listed through the `AgentMetadata` it registered
with. The same metadata becomes the delegation directory when an agent serves
as a subagent, as [the composition post](composing-agents.md) covered.
Every request acts for the user `mash connect` saved, and the code the agents
run for that user runs in their own sandbox. `mash sandbox ls` lists the sandboxes.

## Compose a team

The pool is flat. Which agents work together is your configuration:

```bash
pilot compose my-guide --primary pilot --subagents builder
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
route to its primary, delegation is limited to its subagents, and `/agent`
shows exactly the team you composed. To change the team, exit and re-run
`pilot compose` (define-or-replace), or switch with
`pilot repl --host <other>`.

## The featured app: the guide

`pilot repl --host guide` opens the Mash codebase guide. The `guide` host is
defined in code and is also the default entry in the host config file:

| Agent | Role |
|-------|------|
| `pilot` (primary) | answers questions about Mash from the mounted source, docs and git history |
| `builder` | builds Mash apps in its own workspace and checks them |

```text
> Summarize how HostBuilder composes the agent pool, hosts, and workflows.
> Trace how an accepted request moves through AgentRuntime, RuntimeStore, and RequestEngine.
> Build me an agent with a knowledge base search tool and human approval for refunds.
```

The guide answers from the repository with `rg` and `sed`, run as shell code
through `run_code`,
and its prompt carries a map of where each module and post is documented. When
you ask for an app, it delegates to the builder. The builder follows the
`build-mash-agent`, `build-mash-workflow`, or `build-mash-host` skill, writes the
app under `/home/agent/workspaces/builder/apps/<name>` in your sandbox, installs
mashpy with uv, and runs the app's tests. It cannot serve the app, because a
sandbox has no Docker; copy it out with `docker cp`. The shell renders the subagent trace frames live,
so you watch the delegation happen.

## The changelog workflow

`pilot-changelog` is a two-step [workflow](workflows-as-step-pipelines.md) that
shows the `CodeStep` → `AgentStep` shape end to end. The first step is
deterministic Python: it reads `CHANGELOG.md` and slices out the most recent
release section(s). The second is one agent run that turns that excerpt into a
plain-language summary, reaching into the source with `run_code` only when an entry is
too terse to explain on its own. `guide` attaches it by default, so
`/workflow run pilot-changelog` runs it and streams the steps; add it to your
own host with `--workflows pilot-changelog`.

## Watching it run

After any answer, `/trace` shows where the time went: the timing breakdown,
per-tool stats, and slowest operations from [the trace
post](reading-a-trace.md). For a delegated question the subagent call is its
own span, so you can see how much latency belonged to the builder. The host
also serves the admin dashboard, with the trace drawer and span tree, at
`http://127.0.0.1:8000/admin`.

## Reference

| CLI command | What it does |
|---|---|
| `pilot browse` | catalog listings and the hosts composed on the deployment |
| `pilot compose <id> --primary <agent> [--subagents a,b]` | define-or-replace a composition |
| `pilot hosts` | your saved compositions, with live status |
| `pilot repl --host <id>` | enter a host's REPL, scoped to its team (`--agent <id>` for one bare agent) |

| REPL command | What it does |
|---|---|
| `/agent`, `/host` | list agents and compositions |
| `/history [N]`, `/trace [N]` | recent turns; latency analysis |
| `/workflow list\|run\|status` | inspect or run the workflows attached to this host |
| `/status`, `/session`, `/sessions` | connection and session info |
| `/help`, `/clear`, `/exit` | shell basics |
| `/feedback <message>` | record a note or bug report for the session |

Pilot's CLI is a thin layer over `MashRemoteShell` and `MashHostClient`:
argument parsing, the storefront commands, and local persistence for
compositions. [Building an Agent CLI](building-agent-clis.md) walks through
building the same thing for your own agent.

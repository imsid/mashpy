---
title: The Host API and CLI
description: The HTTP surface a user application integrates with, and the CLI and REPL built on the same client.
date: 2026-06-10
author: imsid
tags:
  - internals
  - interfaces
---

# The Host API and CLI

Everything in this series so far ran inside the host process. This post covers the boundary where applications meet it: an HTTP API under `/api/v1`, and a `mash` CLI whose every command is a client of that same API. Together they are the concrete form of the seam the [product brief](product-brief.md) argues for.

## Three API conventions

The API is REST plus SSE, composed in `src/mash/api`. Three conventions apply across all of it.

**Envelopes.** Success responses arrive as `{"data": ...}`. Errors arrive as `{"error": {"code": ..., "message": ..., "details": ...}}` with stable codes (`AGENT_NOT_FOUND`, `HOST_NOT_FOUND`, `REQUEST_NOT_FOUND`, `OBSERVABILITY_DISABLED`), so client code matches on the code rather than the message.

**Auth.** With no API key configured, routes are open, which suits local development. With `MASH_API_KEY` set, every `/api/v1/*` route requires `Authorization: Bearer <key>` or `X-API-Key: <key>`. The admin dashboard sets a `mash_api_key` cookie on load so the browser SPA can call the protected routes too.

**Discovery.** `GET /` returns service discovery info, `/openapi.json` is the generated schema, and `/docs` serves Swagger UI. `GET /api/v1/health` reports the deployment shape, including the primary agent id and the agent list, and doubles as the liveness and readiness probe in [deployment](how-to-deploy.md).

## The endpoint groups

Each group of endpoints projects one subsystem from earlier in the series.

**Agents.** `GET /agent` lists every agent in the pool with their metadata. `GET /agent/{agent_id}` returns one agent's detail.

**Requests.** The submit-then-stream pair from [the lifecycle post](request-lifecycle.md), plus the operational endpoints around it:

| Method | Path (under `/api/v1`) | Purpose |
|---|---|---|
| POST | `/agent/{agent_id}/request` | submit; returns `request_id`; accepts an optional `structured_output` schema |
| GET | `/agent/{agent_id}/request/{request_id}/events` | SSE stream, a replay of persisted events |
| GET | `/agent/{agent_id}/request/{request_id}/status` | DBOS workflow status, for when the stream goes quiet |
| POST | `/agent/{agent_id}/request/{request_id}/resume` | set a failed request back to pending for recovery |
| POST | `/agent/{agent_id}/request/{request_id}/interaction` | answer an approval or `AskUser` pause (`interaction_id` in the body) |

**Hosts.** Host compositions are managed and used through their own group:

| Method | Path (under `/api/v1`) | Purpose |
|---|---|---|
| PUT | `/hosts/{host_id}` | define or replace a host composition (idempotent) |
| GET | `/hosts` | list all defined hosts |
| GET | `/hosts/{host_id}` | get one host with its primary, subagents, and workflows |
| POST | `/hosts/{host_id}/request` | submit a request through a composed host |

`POST /hosts/{host_id}/request` is the primary submission path when routing through a host. The response includes the primary `agent_id` and `request_id`; events then stream from the existing `GET /agent/{agent_id}/request/{request_id}/events`. Requests snapshot the host composition at submit time, so redefining a host never affects in-flight requests.

**Sessions.** The [memory layer](memory-and-compaction.md), read over HTTP: `GET .../sessions` and `.../sessions/{session_id}` for listings, `.../history` for turns, `.../signals` for per-turn signal payloads with their definitions, and `POST .../compact` to trigger compaction manually. A reasoning endpoint (`GET .../session/{session_id}/trace/{trace_id}/reasoning`) returns the compact step-by-step trace for one turn.

**Publishing.** The dynamic registration from [skills](skills-on-demand.md): `POST /agent/{agent_id}/skill` (idempotent; re-registering a name is a no-op).

**Workflows.** `GET /workflow` lists definition summaries, `GET /workflow/{workflow_id}` returns a complete definition, `POST /workflow/{workflow_id}/run` starts a run with an optional `dedup_key` and `input`, `GET .../runs` pages through run summaries, `GET .../runs/{run_id}` returns one run with its result and step snapshots, and `GET .../runs/{run_id}/events` streams step lifecycle events over SSE.

**Feedback.** Two routes that stay open whether or not observability is enabled. `POST /feedback` records a free-form note with its session context, and `GET /feedback` lists notes for an agent, narrowed by a required `after` timestamp and an optional full-text `q` over the message. The runtime store keeps them in a `runtime_feedback` table beside the event log.

**Telemetry.** Gated by `enable_observability`, with disabled routes returning `503 OBSERVABILITY_DISABLED`:

| Path (under `/api/v1/telemetry`) | Purpose |
|---|---|
| `GET /events` | recent runtime events, paginated by cursor |
| `GET /events/stream` | live SSE tail of incoming runtime events |
| `GET /traces` | recent trace summaries |
| `GET /trace/analysis` | span tree and timing breakdown for one trace |
| `GET /usage` | token and request usage aggregated by time bucket |
| `GET /sessions` | session listing scoped to the runtime store |
| `GET /memory/search` | keyword search over conversation turns |
| `GET /api/events` | backend HTTP request log (when API logging is enabled) |
| `POST /api/events/search` | filtered search over the HTTP request log |
| `GET /api/events/stream` | live SSE tail of the HTTP request log |
| `POST /command-events` | ingest CLI command lifecycle events from the REPL |
| `GET /command-events` | list ingested command events |

## One client underneath

`MashHostClient` in `mash.cli` wraps the API one method per endpoint: `health()`, `submit_request(...)`, `stream_request(...)`, `post_interaction(...)`, `run_workflow(...)`, `stream_workflow_run(...)`, `get_trace_analysis(...)`, and so on. The streaming methods parse SSE internally and yield frames as dicts, so consumers iterate events without touching the wire format.

The `mash` CLI is built entirely on this client. Nothing in it has a private path into the runtime; `mash status` calls `GET /api/v1/health`, and every frame the REPL renders arrived on the same SSE stream your application would read. Driving the CLI against a host exercises exactly the protocol your integration will use.

## The mash command

`mash connect` persists a default connection (base URL, API key, and a target: a bare `--agent` or an existing `--host` composition), and later commands resolve their target from flags first, then environment (`MASH_API_BASE_URL`, `MASH_API_KEY`), then that saved config. `mash compose --host <id> --primary <agent>` (with optional `--subagents` and `--workflows`) defines the composition on the deployment with an idempotent `PUT` and pins it as the target. With a connection in place, five one-shot commands cover quick checks: `mash status`, `mash browse`, `mash agents`, `mash hosts`, `mash sessions`, and `mash history --session-id ...`.

`mash browse` shows the full pool in one view: agents, workflows, and defined hosts. `mash agents` and `mash hosts` list each separately.

`mash host serve` is the other side of the boundary: it loads `build_pool()` from `--host-app` (or `MASH_HOST_APP`) and runs the API server, with flags for bind host, port, API key, CORS origins, and disabling observability.

## The REPL

`mash repl` opens the interactive shell. Plain input becomes a submit-and-stream round trip; lines starting with `/` are commands that run locally:

| Command | What it does |
|---|---|
| `/status` | deployment and connection info |
| `/agent` | list hosted agents (host members only when connected through a host) |
| `/host` | list the host compositions defined on the deployment |
| `/session`, `/sessions` | current session info, session list |
| `/history [N]` | recent turns |
| `/trace [N]` | trace analysis for recent traces |
| `/feedback <message>` | record a note or bug report for the session |
| `/workflow list|run|status` | workflow operations |
| `/help`, `/clear`, `/exit` | shell basics |

Two parts of the shell do more than print frames.

Interactions render as prompts. When a `request.interaction.create` frame arrives, the shell prompts for an approval decision, a choice, or free-form input, and POSTs the answer back through the interaction endpoint, so the [human-in-the-loop flow](human-in-the-loop.md) works end to end at the terminal.

Streaming renders as formatted markdown. The chain renderer buffers `llm.response.delta` chunks and flushes each completed markdown block as it finishes, so answers stream in with headings and highlighted code instead of raw text, and unterminated code fences stay buffered until they close. The shell then suppresses the end-of-turn re-render when tokens already streamed, so the response appears exactly once.

## Building on it

The same pieces are public for your own interface: `MashHostClient` for any Python application, and `MashRemoteShell`, `Command`, and `CLIContext` for a CLI with your own slash commands. [Building an Agent CLI](building-agent-clis.md) walks through that end to end, and [the deploy guide](how-to-deploy.md) covers putting the API on the network.

Every request that crosses this surface leaves runtime events behind. The next post covers the pauses for people built into the loop: tool approval and `AskUser`.

*Next: [Human-in-the-Loop](human-in-the-loop.md).*

---
title: How to Deploy a Mash Host
description: Deploy a Mash host and its agent computers with Docker, on your laptop or a server.
date: 2026-06-02
author: imsid
tags:
  - deploy
  - ops
---

# How to Deploy a Mash Host

This guide covers deploying a Mash host and its agents, on your laptop or on a
server with Docker. The steps apply to any Mash application; the examples use
`my_agent.spec:build_pool`.

## Architecture

Every agent in the pool runs on its own computer: a container that serves the
agent's runtime behind an H2A HTTP server, with its memory and workspace on
named volumes. The host runs the API, host compositions and workflows, and
reaches each agent over HTTP.

```
 user app ──HTTP──► Mash host (API, hosts, workflows)
                     │  one AgentClient per agent
                     │  starts missing computers through the Docker socket
                     ▼ H2A HTTP              ▲ /h2a relay (subagent calls)
   ┌──────────────────────────────┐          │
   │ mash-agent-research          │──────────┘
   │  mash agent serve :9000      │
   │  /home/agent/memory     ◄── volume research-memory
   │  /home/agent/workspace  ◄── volume research-workspace
   └──────────────┬───────────────┘
                  ▼
     Postgres: events, DBOS, sessions, workflow runs
```

- The host and every computer run the same image. The host imports
  `build_pool()` and keeps each agent's spec and metadata; each computer
  imports the same `build_pool()` and serves one agent from it.
- When the host starts, it makes sure each registered agent has a running
  container named `mash-agent-<agent_id>`. A crashed container is restarted
  by Docker (restart policy `always`) with the same volumes.
- Containers outlive the host. A restarted host finds them running and leaves
  their in-flight requests alone.
- A primary calls its subagents through the host's `/h2a` relay, which checks
  the call against the request's host composition. Computers know the host's
  address and nothing else.
- Each computer's bash sessions run as an `agent` user with a clean
  environment, so the agent cannot read the runtime's database URL or API
  keys.
- All durable state (events, sessions, DBOS workflows, workflow runs) lives in
  Postgres. Memory and workspace files live on the volumes.

A deployment runs one host replica.

## Prerequisites

- Docker Engine on the machine that runs the host. The host starts computers
  through the Docker socket, so platforms without a Docker daemon (Render,
  Cloud Run, Fargate) cannot run agent computers.
- PostgreSQL 14 or later, reachable from the host and from the computers
- An API key for your LLM provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
  `GEMINI_API_KEY`)
- A host secret in `MASH_HOST_SECRET`. The host derives each agent's token
  from it, so keep it stable across restarts:

```bash
export MASH_HOST_SECRET=$(openssl rand -hex 32)
```

## The image

Computers run the host's own image, so the image must contain your app and
the two users a computer needs: `mash` runs the runtime, and `agent` runs
bash. `mash` may start bash as `agent` and nothing else.

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MASH_DATA_DIR=/var/lib/mash \
    MASH_API_HOST=0.0.0.0 MASH_API_PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends sudo \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash mash \
    && useradd --create-home --shell /bin/bash agent \
    && chmod 0700 /home/mash \
    && mkdir -p /home/agent/memory /home/agent/workspace \
    && chown -R agent:agent /home/agent \
    && chmod 0755 /home/agent /home/agent/memory /home/agent/workspace \
    && echo "mash ALL=(agent) NOPASSWD: /bin/bash" > /etc/sudoers.d/mash-agent \
    && chmod 0440 /etc/sudoers.d/mash-agent \
    && mkdir -p /var/lib/mash && chown mash:mash /var/lib/mash

WORKDIR /opt/app
COPY . .
# Your app, with mashpy as a dependency.
RUN pip install .

USER mash
CMD ["sh", "-c", "mash host serve --host-app \"$MASH_HOST_APP\" --host \"$MASH_API_HOST\" --port \"$MASH_API_PORT\""]
```

The repository's own [`Dockerfile`](https://github.com/imsid/mashpy/blob/main/Dockerfile)
has the same layout.

## Run the host in Docker

This is the production layout, and it works the same on a laptop. The host
container joins the private network `mash`, reaches computers by container
name, and starts them with its own image id.

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: mash
      POSTGRES_USER: mash
      POSTGRES_PASSWORD: mash
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks: [mash]
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "mash"]
      interval: 3s
      retries: 5

  host:
    build: .
    environment:
      MASH_HOST_APP: my_agent.spec:build_pool
      MASH_DATABASE_URL: postgresql://mash:mash@postgres:5432/mash
      MASH_HOST_SECRET: ${MASH_HOST_SECRET}
      MASH_API_KEY: ${MASH_API_KEY:-dev-key}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    # The image runs as `mash`; the socket belongs to root's group (0) on
    # Docker Desktop and usually to the `docker` group on Linux.
    group_add: ["0"]
    ports:
      - "127.0.0.1:8000:8000"
    networks: [mash]
    depends_on:
      postgres:
        condition: service_healthy

networks:
  mash:
    name: mash

volumes:
  pgdata:
```

```bash
docker compose up -d
mash connect --api-base-url http://127.0.0.1:8000 --api-key dev-key
mash repl --agent assistant
```

On Linux, find the socket's group with `stat -c %g /var/run/docker.sock` and
use that number in `group_add`.

The host passes its environment through to every computer, minus process
variables (`PATH`, `HOME`, and so on) and its own secrets (`MASH_HOST_SECRET`,
`MASH_API_KEY`). Provider keys, MCP header tokens and anything else a spec
reads from the environment reach the computer this way.

## Run the host on your laptop

For a faster loop you can run `mash host serve` directly while the computers
run in Docker. Two things differ:

- Computers can't run the host's image, because there is none. Build an image
  of your app and name it in `MASH_COMPUTER_IMAGE`. Rebuild it after code
  changes, then replace the computers (see Operating computers).
- Each computer's port is published on `127.0.0.1` at a port fixed when its
  container is created, and computers reach the host at
  `host.docker.internal`.

```bash
docker build -t my-agent:dev .
export MASH_COMPUTER_IMAGE=my-agent:dev
export MASH_DATABASE_URL=postgresql://mash:mash@host.docker.internal:5432/mash
mash host serve --host-app my_agent.spec:build_pool --port 8000
```

Environment values pass through unchanged, so any address the computers use
must work from inside a container: write `host.docker.internal` rather than
`localhost` in `MASH_DATABASE_URL` and MCP server URLs. On Linux, also bind the
host to `0.0.0.0` (`--host 0.0.0.0`) so computers can reach it through
`host.docker.internal`.

## Deploy to a server

Run the same compose layout on a VM with Docker (EC2, Compute Engine, a
Hetzner box) and point `MASH_DATABASE_URL` at managed Postgres if you like.

1. Provision Postgres 14+ and note the connection string. The computers
   connect to it too, so it must be reachable from the Docker network.
2. Build the image on the VM, or build it elsewhere and pull it.
3. Set `MASH_HOST_APP`, `MASH_DATABASE_URL`, `MASH_HOST_SECRET`, `MASH_API_KEY`
   and your provider keys in the host's environment.
4. Start the host with the socket mounted and `group_add` set, as in the
   compose file above.
5. Put TLS in front of port 8000. For SSE, disable response buffering and set
   the idle timeout to at least 3600s.

CPU, memory and disk limits, and outbound network rules for the computers, are
set on the Docker daemon and network. Mash only decides which containers
exist.

## Operating computers

These commands run on the Docker host and find computers by their
`mash.agent` label:

```bash
mash computer ls                   # status, uptime, restart count, volumes
mash computer shell research       # bash as `agent` in /home/agent
mash computer shell research --root
mash computer restart research     # replace the container, keep its volumes
```

- `restart` recreates the container from its own configuration. Memory and
  workspace survive; anything else on the container layer (apt installs,
  files outside the two mounts) is gone. An agent that needs system packages
  across replacements records the install steps in memory or keeps a venv in
  its workspace.
- Commands run from an admin shell are not in the event log. Their history is
  appended to `/home/agent/workspace/.admin_history`.
- A call to an agent whose computer is down returns `503 AGENT_UNAVAILABLE`
  from host routes. The next host start brings stopped computers back.
- To pick up a new image, restart the host and then `mash computer restart`
  each agent. Running containers are left alone on host start, so a new image
  reaches a computer only when its container is replaced.

## Durability

- Each computer runs DBOS as executor `agent:<agent_id>`, so after a crash or
  replacement it recovers exactly its own agent's pending requests. The host
  runs as executor `host` and recovers only its workflow runs.
- Subagent calls and workflow agent steps submit with fixed request ids, so a
  replayed call reattaches to the request it already started.
- The host is safe to restart at any time. Requests in flight on computers
  keep running and their events stay in Postgres.

## Accessing the host from applications

The Mash host exposes a REST + SSE API under `/api/v1`. Any HTTP client can
interact with it.

### Authentication

If `MASH_API_KEY` is set, include it in requests:

```
Authorization: Bearer <api-key>
X-API-Key: <api-key>          # alternative header
```

The `/h2a` relay is for agent computers only and authenticates with agent
tokens, not the API key. It does not need to be reachable from outside the
Docker network.

### Core API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Health check and agent info |
| `GET` | `/api/v1/agent` | List registered agents |
| `POST` | `/api/v1/agent/{agent_id}/request` | Submit a request |
| `GET` | `/api/v1/agent/{agent_id}/request/{request_id}/events` | Stream response (SSE) |
| `POST` | `/api/v1/agent/{agent_id}/request/{request_id}/interaction` | Respond to an interaction (e.g. AskUser); `interaction_id` goes in the body |

### Example: submit a request and stream the response

```bash
# 1. Submit a request
REQUEST=$(curl -s -X POST http://localhost:8000/api/v1/agent/assistant/request \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "summarize the open issues", "session_id": "my-session"}')

REQUEST_ID=$(echo $REQUEST | jq -r '.data.request_id')

# 2. Stream the response via SSE
curl -N http://localhost:8000/api/v1/agent/assistant/request/$REQUEST_ID/events \
  -H "Authorization: Bearer dev-key"
```

### Example: Python client

```python
import httpx

BASE = "http://localhost:8000/api/v1"
HEADERS = {"Authorization": "Bearer dev-key"}

# Submit
resp = httpx.post(
    f"{BASE}/agent/assistant/request",
    headers=HEADERS,
    json={"message": "summarize the open issues", "session_id": "s1"},
)
request_id = resp.json()["data"]["request_id"]

# Stream SSE
with httpx.stream(
    "GET",
    f"{BASE}/agent/assistant/request/{request_id}/events",
    headers=HEADERS,
    timeout=None,
) as stream:
    for line in stream.iter_lines():
        if line.startswith("data:"):
            print(line)
```

### Example: connect with the Mash CLI

```bash
mash connect \
  --api-base-url http://your-host:8000 \
  --api-key your-api-key
mash repl --agent assistant
```

### CORS configuration

By default, the host allows requests from `localhost:3000` and
`localhost:5173`. For production, set allowed origins via the `--cors-origin`
CLI flag (repeatable) or configure `cors_allow_origins` in `MashHostConfig`.

## Tearing down and restarting

- **Restart the host:** the new process calls `build_pool()`, reconnects to
  Postgres, finds the running computers, and starts serving.
- **Stop everything:** stop the host, then `docker rm -f` the
  `mash-agent-*` containers. Postgres and the volumes keep all state; the next
  host start recreates the containers on the same volumes.
- **Remove an agent's memory and workspace:** delete its two volumes,
  `<agent_id>-memory` and `<agent_id>-workspace`, after removing its container.
- **Database migrations:** Mash runs pending migrations automatically on
  startup. See [Schema migrations](#schema-migrations) below.

## Schema migrations

Mash uses an ordered-file migration runner rather than a third-party tool. All Mash tables (the runtime event store, the memory store, and the evals store) share one schema baseline in `src/mash/storage/migrations/`, and every store runs the same runner when it opens. On each run the runner:

1. Takes a Postgres advisory lock, so stores opening concurrently apply each migration exactly once.
2. Creates a `_mash_migrations` tracking table if it does not exist.
3. Reads all `.sql` files from `src/mash/storage/migrations/` in filename order.
4. Applies any file not yet recorded in `_mash_migrations` and records the filename and timestamp so it is not applied again. The whole run is one transaction.

The baseline (`001_baseline.sql`) is idempotent: all `CREATE TABLE` and `CREATE INDEX` statements use `IF NOT EXISTS`, so applying it against an existing database is a no-op.

**Adding a migration:** Create a new file named `NNN_description.sql` (e.g. `002_add_model_column.sql`) in `src/mash/storage/migrations/`. The next startup applies it automatically. Write it to be rollback-safe.

**Upgrading from an earlier deployment:** Releases up to 0.15 tracked runtime and memory migrations separately, in `_mash_migrations` and `_mash_memory_migrations`. The combined baseline applies cleanly on top of a database created by those runners: every statement is guarded, so existing tables and data are untouched, and the old `_mash_memory_migrations` table is simply left behind. `MASH_DATABASE_URL` is required; the agent raises `RuntimeError` at startup if it is unset.

## Environment variables

| Variable | Set on | Default | Purpose |
|----------|--------|---------|---------|
| `MASH_HOST_APP` | host | none | `module:attribute` for `build_pool()`; computers import the same one |
| `MASH_DATABASE_URL` | host | none | PostgreSQL connection string; passed to computers |
| `MASH_HOST_SECRET` | host | none | Derives each agent's token; required for a pool with agents |
| `MASH_API_KEY` | host | none | API key for `/api/v1` |
| `MASH_API_HOST` | host | `127.0.0.1` | Bind host (`0.0.0.0` in containers) |
| `MASH_API_PORT` | host | `8000` | Bind port |
| `MASH_COMPUTER_IMAGE` | host | the host's own image | Image for computers; required when the host runs outside a container |
| `MASH_COMPUTER_NETWORK` | host | `mash` | Docker network the host and computers share |
| `MASH_COMPUTER_HOST_API_URL` | host | derived | Override the host address computers call |
| `MASH_DATA_DIR` | host | `/var/lib/mash` | Persistent data directory |
| `MASH_RUNNER_ID` | host | `default` | Workflow runner identity for this deployment |
| `MASH_AGENT_TOKEN` | computer | set by the host | The computer's agent token |
| `MASH_HOST_API_URL` | computer | set by the host | The host address for subagent calls |
| `MASH_MEMORY_DIR` | computer | `/home/agent/memory` | File memory directory, for agents with `memory_enabled` |
| `ANTHROPIC_API_KEY` | host | none | Anthropic API key; passed to computers |
| `OPENAI_API_KEY` | host | none | OpenAI API key; passed to computers |
| `GEMINI_API_KEY` | host | none | Google Gemini API key; passed to computers |
| `DBOS_CONDUCTOR_KEY` | host | none | DBOS Conductor key; passed to computers |

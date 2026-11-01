---
title: How to Deploy a Mash Host
description: Deploy a Mash host, its agent processes, and per-user sandboxes, on your laptop or a server.
date: 2026-06-02
author: imsid
tags:
  - deploy
  - ops
---

# How to Deploy a Mash Host

This guide covers deploying a Mash host and its agents, on your laptop or on a
server. The steps apply to any Mash application; the examples use
`my_agent.spec:build_pool`.

## Architecture

A Mash host is the unit of deploy: one container or one local process. It
runs the API, host compositions and workflows, and starts one child process
per agent. Each child serves its agent's runtime behind an H2A HTTP server on
a localhost port. The code an agent's model writes runs in a sandbox that
belongs to the user the request acts for, and the user opens what it made at
links the host serves.

```
 user app ──HTTP──► Mash host (API, hosts, workflows)
                     │  one child per agent, on 127.0.0.1:
                     │    mash agent serve --agent research
                     │  /h2a relay (subagent calls), /sandbox relay (run_code,
                     │  sandbox_put, sandbox_get), /s/ links (users)
                     │
                     ▼ Docker socket
   ┌──────────────────────────────────────┐
   │ mash-sandbox-<key>   (user u-42)     │
   │  mash sandbox serve :9000            │
   │  /home/agent/workspaces/research  ◄── volume mash-sandbox-<key>
   └──────────────────────────────────────┘

   Postgres: events, DBOS, sessions, workflow runs (host and agents only)
```

- The host imports `build_pool()` and keeps each agent's spec and metadata.
  Each agent process imports the same `build_pool()` and serves one agent from
  it. Agent processes are the app's own code and inherit the host's
  environment.
- A child that exits is restarted on the same port, with backoff from 1 to 30
  seconds. While it is down, host routes for that agent return
  `503 AGENT_UNAVAILABLE`. Children live and die with the host.
- A primary calls its subagents through the host's `/h2a` relay, which checks
  the call against the request's host composition.
- Every request carries a `user_id`. A `run_code`, `sandbox_put` or
  `sandbox_get` call goes from the agent process to the host's `/sandbox`
  relay with only the request id; the host looks up the request's user and acts
  in that user's sandbox, in the agent's folder
  `/home/agent/workspaces/<agent_id>`.
- A sandbox holds one user's workspaces and nothing else. It gets no
  environment from the host and has no database credentials. Code runs as an
  `agent` user with a clean environment. The sandbox's daemon accepts only that
  user's sandbox token, which only the host can derive.
- Files in a workspace and servers the code started open at
  `/s/<link token>/files/<agent_id>/<path>` and `/s/<link token>/ports/<n>/`
  on the host. The link token names the user, so links need no API key.
- Sandboxes start on first use and stop after `MASH_SANDBOX_IDLE_SECONDS`
  (default 900) without a call or a link request. At most `MASH_SANDBOX_MAX_RUNNING` (default
  20) run at once.
- A host that cannot reach a Docker daemon runs without sandboxes, and its
  agents get no sandbox tools. This is how a host runs on a platform with no
  Docker socket (Render, Cloud Run, Fargate). The host logs which mode it chose.
- All durable state (events, sessions, DBOS workflows, workflow runs) lives in
  Postgres. Workspace files live on the sandbox volumes.

A deployment runs one host replica.

## Prerequisites

- PostgreSQL 14 or later, reachable from the host
- An API key for your LLM provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
  `GEMINI_API_KEY`)
- A host secret in `MASH_HOST_SECRET`. The host derives each agent's and each
  sandbox's token from it, so keep it stable across restarts:

```bash
export MASH_HOST_SECRET=$(openssl rand -hex 32)
```

- For sandboxes: Docker Engine on the machine that runs the host.

## The images

The host runs from an image that contains your app:

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MASH_DATA_DIR=/var/lib/mash \
    MASH_API_HOST=0.0.0.0 MASH_API_PORT=8000

RUN useradd --create-home --shell /bin/bash mash \
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

Sandboxes run Mash's sandbox image, `ghcr.io/imsid/mash-sandbox:<mashpy
version>`, built from [`Dockerfile.sandbox`](https://github.com/imsid/mashpy/blob/main/Dockerfile.sandbox).
It holds Python 3.12 with pip and uv, Node 22 with npm, SQLite, git, curl, jq
and rg, and the two users a sandbox needs: `mash` runs the sandbox daemon, and
`agent` runs the model's code. The host pulls it on first start. Data libraries
are not preinstalled; the model installs what it needs with
`pip install --user`, which lands in the agent's folder and persists. To
preinstall more, build on it and name your image in `MASH_SANDBOX_IMAGE`:

```dockerfile
FROM ghcr.io/imsid/mash-sandbox:<version>
USER root
RUN pip install pandas matplotlib
USER mash
```

Browsers run Mash's browser image, `ghcr.io/imsid/mash-browser:<mashpy
version>`, built from [`Dockerfile.browser`](https://github.com/imsid/mashpy/blob/main/Dockerfile.browser).
It holds Chromium, Playwright MCP, Xvfb, x11vnc and noVNC, run by a `browser`
user. The host pulls it only when an agent's spec lists `browser_sites()`, and
starts one browser per user on the first browser call, on its own network,
`mash-browsers`, with a volume `mash-browser-<key>` for the user's profile.
Sandboxes are not on that network. A sign-in link (`/s/<view token>/browser/`)
uses `MASH_PUBLIC_URL`, and the live view is a websocket, so a proxy in front
of the host must pass websockets through.

## Run the host in Docker

This is the production layout, and it works the same on a laptop. The host
container creates the network `mash-sandboxes`, joins it, and reaches each
sandbox by container name.

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
      # Where users' browsers reach the host, for sandbox links.
      MASH_PUBLIC_URL: http://127.0.0.1:8000
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
mash connect --api-base-url http://127.0.0.1:8000 --api-key dev-key --user-id u-1
mash repl --agent assistant
```

On Linux, find the socket's group with `stat -c %g /var/run/docker.sock` and
use that number in `group_add`. Without the socket, the host runs without
sandboxes.

Agent processes inherit the host's environment, so provider keys, MCP header
tokens and anything else a spec reads reach them. `MASH_HOST_SECRET` and
`MASH_API_KEY` are the exceptions: they stay with the host, and each agent
authenticates with a token of its own. MCP URLs resolve inside the
host container. Sandboxes get none of the host's environment.

To give every sandbox files from the Docker host, list bind mounts in
`MASH_SANDBOX_MOUNTS`, for example
`MASH_SANDBOX_MOUNTS=/srv/docs:/home/agent/docs:ro`. The source is a path on
the Docker host, even when the host runs in a container, because the daemon
resolves it. A target inside `/home/agent/workspaces` sits on top of the
user's volume without being copied into it. Bound files keep their owner on the
Docker host, so add `:ro` unless the `agent` user can write them.

## Run the host on your laptop

For a faster loop you can run `mash host serve` directly. Its agents run as
its children on your machine, so `localhost` addresses work as they are in
`MASH_DATABASE_URL` and MCP server URLs.

Sandboxes still run in Docker, from the same sandbox image; each sandbox's
daemon port is published on `127.0.0.1`. Links use the bind address, so they
open in your browser as they are. Without Docker, the host runs and its agents
get no sandbox tools.

```bash
export MASH_DATABASE_URL=postgresql://mash:mash@localhost:5432/mash
mash host serve --host-app my_agent.spec:build_pool --port 8000
```

## Deploy to a server

Run the same compose layout on a VM with Docker (EC2, Compute Engine, a
Hetzner box) and point `MASH_DATABASE_URL` at managed Postgres if you like. On
a platform without Docker, run the host container alone; it serves every agent
without sandbox tools.

1. Provision Postgres 14+ and note the connection string. Only the host's
   container connects to it.
2. Build the image on the VM, or build it elsewhere and pull it.
3. Set `MASH_HOST_APP`, `MASH_DATABASE_URL`, `MASH_HOST_SECRET`, `MASH_API_KEY`,
   `MASH_PUBLIC_URL` (the HTTPS address users reach the host at) and your
   provider keys in the host's environment.
4. Start the host with the socket mounted and `group_add` set, as in the
   compose file above.
5. Put TLS in front of port 8000. For SSE, disable response buffering and set
   the idle timeout to at least 3600s.

CPU, memory and disk limits for sandboxes are set on the Docker daemon, and
outbound rules on the `mash-sandboxes` network. Mash only decides which
sandboxes exist.

## Operating agents

The host writes each agent process's output to its own log, one line at a
time, prefixed with `[<agent_id>]`.

Every agent `build_pool()` registers starts enabled. Disable one to stop its
process and enable it to start it again:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/agent/research/disable -H "Authorization: Bearer dev-key"
curl -X POST http://127.0.0.1:8000/api/v1/agent/research/enable -H "Authorization: Bearer dev-key"
curl http://127.0.0.1:8000/api/v1/agent -H "Authorization: Bearer dev-key"   # enabled, status
```

- Disable returns `409 AGENT_IN_USE`, naming the references, while a host
  composition or workflow names the agent.
- Requests in flight are not cancelled. They stay pending and continue when
  the agent is enabled again. New requests to a disabled agent return
  `409 AGENT_DISABLED`.
- The enabled set is in memory and resets on restart. Adding an agent that is
  not in `build_pool()` takes a deploy.

## Operating sandboxes

These commands run on the Docker host and find sandboxes by their
`mash.sandbox.user` label. For a namespaced deployment, pass `--namespace` or
set `MASH_SANDBOX_NAMESPACE`.

```bash
mash sandbox ls                               # user, status, uptime, container
mash sandbox shell u-42 --agent research      # a shell as `agent` in research's folder
mash sandbox shell u-42 --root
mash sandbox stop u-42                        # stop now; the volume is kept
```

- `shell` starts a stopped sandbox. Commands run from it are not in the event
  log; their history is appended to `/home/agent/workspaces/.admin_history`.
- Anything outside `/home/agent/workspaces` is on the container layer and is
  lost when the container is replaced. `pip install --user` and `npm install`
  install into the agent's folder, so packages persist.
- Background processes the model left running stop with the sandbox, so a port
  link fails until the code starts its server again. File links keep working.
- A sandbox keeps the image it was created from. To pick up a new image,
  remove the containers with
  `docker rm -f $(docker ps -aq --filter label=mash.sandbox.user)`; each user's
  next call creates a new one on the same volume.

## Durability

- Each agent process runs DBOS as executor `agent:<agent_id>`, so after a
  crash, a restart, or being enabled again it recovers exactly its own agent's
  pending requests. The host runs as executor `host` and recovers only its
  workflow runs.
- Subagent calls and workflow agent steps submit with fixed request ids, so a
  replayed call reattaches to the request it already started.
- A `run_code` call is a tool step. After a crash DBOS replays it, and the code
  runs again in the same sandbox.
- A host restart restarts every agent process, and each recovers its pending
  requests. Running sandboxes count as active from the restart.

## Exporting traces

The host can send every request's trace to an OpenTelemetry backend over OTLP.
Install mashpy with the `otel` extra in the host image and point it at a
collector:

```bash
uv add 'mashpy[otel]'
```

```yaml
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
      OTEL_SERVICE_NAME: assistant
```

The exporter runs in the host and is off until an endpoint is set. It exports a
request when it ends: its spans (`invoke_agent`, `chat`, `execute_tool`,
`mash.*`), its log records (`exception`, `mash.request.resumed`,
`mash.request.mode_changed`), and the two GenAI client metrics. A workflow run
exports as one trace when it finishes, with each agent step's request under its
step. The exporter keeps its position in `otel_export_cursor`, so a restart
picks up where it stopped. The first time it runs it starts at the end of the
log, so requests and runs that finished before you set the endpoint are not
exported.

Headers, protocol and timeouts come from the standard `OTEL_EXPORTER_OTLP_*`
variables. Prompts, completions and tool arguments and results stay out of the
export unless `MASH_OTEL_CAPTURE_CONTENT=1` is set. The admin UI shows the
same spans whether or not export is on.

## Accessing the host from applications

The Mash host exposes a REST + SSE API under `/api/v1`. Any HTTP client can
interact with it.

### Authentication

If `MASH_API_KEY` is set, include it in requests:

```
Authorization: Bearer <api-key>
X-API-Key: <api-key>          # alternative header
```

The `/h2a` and `/sandbox` relays are for the host's own agent processes and
authenticate with agent tokens, not the API key. They share the host's port;
a proxy in front of the host can block both paths from outside. The `/s/`
links are for users and must stay reachable; the token in each link is its
only credential.

### Core API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Health check and agent info |
| `GET` | `/api/v1/agent` | List registered agents with `enabled` and `status` |
| `POST` | `/api/v1/agent/{agent_id}/request` | Submit a request; `user_id` is required |
| `GET` | `/api/v1/agent/{agent_id}/request/{request_id}/events` | Stream response (SSE) |
| `POST` | `/api/v1/agent/{agent_id}/request/{request_id}/interaction` | Respond to an interaction (e.g. AskUser); `interaction_id` goes in the body |
| `POST` | `/api/v1/agent/{agent_id}/enable` | Start a disabled agent |
| `POST` | `/api/v1/agent/{agent_id}/disable` | Stop an agent |

### Example: submit a request and stream the response

```bash
# 1. Submit a request
REQUEST=$(curl -s -X POST http://localhost:8000/api/v1/agent/assistant/request \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "summarize the open issues", "session_id": "my-session", "user_id": "u-42"}')

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
    json={"message": "summarize the open issues", "session_id": "s1", "user_id": "u-42"},
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
  --api-key your-api-key \
  --user-id u-42
mash repl --agent assistant
```

### CORS configuration

By default, the host allows requests from `localhost:3000` and
`localhost:5173`. For production, set allowed origins via the `--cors-origin`
CLI flag (repeatable) or configure `cors_allow_origins` in `MashHostConfig`.

## Tearing down and restarting

- **Restart the host:** the new process calls `build_pool()`, reconnects to
  Postgres, starts every agent process again, and adopts the running
  sandboxes.
- **Stop everything:** stop the host, then
  `docker rm -f $(docker ps -aq --filter label=mash.sandbox.user)`. Postgres
  and the sandbox volumes keep all state; each user's next call recreates
  their sandbox on the same volume.
- **Remove a user's workspaces:** delete their volume, which has the
  container's name from `mash sandbox ls`, after removing the container.
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
| `MASH_HOST_APP` | host | none | `module:attribute` for `build_pool()`; agent processes import the same one |
| `MASH_DATABASE_URL` | host | none | PostgreSQL connection string; agent processes inherit it |
| `MASH_HOST_SECRET` | host | none | Derives each agent's, each sandbox's and each browser's token, and signs links; required for a pool with agents |
| `MASH_API_KEY` | host | none | API key for `/api/v1` |
| `MASH_API_HOST` | host | `127.0.0.1` | Bind host (`0.0.0.0` in containers) |
| `MASH_API_PORT` | host | `8000` | Bind port |
| `MASH_PUBLIC_URL` | host | the bind address | Where users reach the host; the base of sandbox links and sign-in links |
| `MASH_SANDBOX_IMAGE` | host | `ghcr.io/imsid/mash-sandbox:<mashpy version>` | Sandbox image, pulled when missing |
| `MASH_SANDBOX_MOUNTS` | host | none | Comma-separated `source:target[:ro]` bind mounts added to every sandbox; sources are Docker-host paths |
| `MASH_SANDBOX_IDLE_SECONDS` | host | `900` | Idle time before a sandbox stops |
| `MASH_SANDBOX_MAX_RUNNING` | host | `20` | Most sandboxes running at once |
| `MASH_SANDBOX_NAMESPACE` | host, CLI | `mash` | Prefix for sandbox and browser containers, volumes and networks (`<ns>-sandbox-<key>`, `<ns>-sandboxes`, `<ns>-browser-<key>`, `<ns>-browsers`), so deployments sharing a Docker daemon keep them separate |
| `MASH_BROWSER_IMAGE` | host | `ghcr.io/imsid/mash-browser:<mashpy version>` | Browser image, pulled when an agent lists sites |
| `MASH_BROWSER_IDLE_SECONDS` | host | `900` | Idle time before a browser stops; its profile is kept |
| `MASH_BROWSER_MAX_RUNNING` | host | `10` | Most browsers running at once |
| `MASH_BROWSER_HANDOFF_SECONDS` | host | `900` | How long a sign-in link works, and how long a request waits for the user |
| `MASH_DATA_DIR` | host | `/var/lib/mash` | Persistent data directory |
| `MASH_RUNNER_ID` | host | `default` | Workflow runner identity for this deployment |
| `MASH_AGENT_TOKEN` | agent process | set by the host | The agent's token |
| `MASH_HOST_API_URL` | agent process | set by the host | The host address for subagent calls and the sandbox tools |
| `MASH_SANDBOX_ENABLED` | agent process | set by the host | `1` when the host has sandboxes, so the agent gets `run_code`, and `sandbox_put` and `sandbox_get` when its spec declares file roots |
| `MASH_BROWSER_ENABLED` | agent process | set by the host | `1` when the host has browsers, so an agent whose spec lists sites gets the browser tools |
| `MASH_PARENT_PID` | agent process | set by the host | The agent process stops when this process exits |
| `MASH_SANDBOX_TOKEN` | sandbox | set by the host | The user's sandbox token; the sandbox's only variable |
| `MASH_BROWSER_TOKEN` | browser | set by the host | The user's browser token; the browser's only variable |
| `ANTHROPIC_API_KEY` | host | none | Anthropic API key; agent processes inherit it |
| `OPENAI_API_KEY` | host | none | OpenAI API key; agent processes inherit it |
| `GEMINI_API_KEY` | host | none | Google Gemini API key; agent processes inherit it |
| `DBOS_CONDUCTOR_KEY` | host | none | DBOS Conductor key; agent processes inherit it |
| `CORTEX_AGENT_<PROVIDER>_MODEL` | host | the provider's default | Model for `cortex-agent` in a pool with Cortex enabled (`ANTHROPIC`, `OPENAI`, `GEMINI`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | host | none | OTLP/HTTP collector address; setting it (or a per-signal `OTEL_EXPORTER_OTLP_{TRACES,LOGS,METRICS}_ENDPOINT`) turns the exporter on. Needs `mashpy[otel]` |
| `OTEL_EXPORTER_OTLP_HEADERS` | host | none | Headers for the collector, such as an API key |
| `OTEL_SERVICE_NAME` | host | `mash` | `service.name` on every exported span, log record and metric |
| `MASH_OTEL_CAPTURE_CONTENT` | host | off | `1` adds prompts, completions and tool arguments and results to the export |

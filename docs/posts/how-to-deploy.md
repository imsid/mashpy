---
title: How to Deploy a Mash Host
description: Deploy a Mash Host with one or more agents, on your laptop, in Docker, or on any cloud provider.
date: 2026-06-02
author: imsid
tags:
  - deploy
  - ops
---

# How to Deploy a Mash Host

This guide covers deploying a Mash Host with one or more agents, on your
laptop, in Docker, or on any cloud provider. It uses the
Pilot agent ([`src/pilot/`](https://github.com/imsid/mashpy/tree/main/src/pilot/)) as a running example but the steps apply to any
Mash application.

## Architecture Overview

Mash follows the **Host → Client → AgentRuntime** model (H2A protocol):

```
┌────────────────────────────────────────────────────┐
│                    Mash Host                       │
│                                                    │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐    │
│  │  Primary   │  │ Subagent A │  │ Subagent B │    │
│  │  Agent     │  │            │  │            │    │
│  │  Runtime   │  │  Runtime   │  │  Runtime   │    │
│  └─────┬──────┘  └─────▲──────┘  └─────▲──────┘    │
│        │               │               │           │
│        └── InProcessAgentClient ───────┘           │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │           FastAPI Server (/api/v1)           │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────┬───────────────────────────────┘
                     │
                     ▼
              ┌──────────────┐
              │  PostgreSQL  │
              │  (external)  │
              └──────────────┘
```

A single Host process manages all agents in-process. The primary agent
delegates to subagents via `InvokeSubagent`; these calls are in-memory
function calls, not network hops. All durable state (events, memory, DBOS
workflows) lives in PostgreSQL.

## Prerequisites

- Python >= 3.10
- PostgreSQL >= 14 (required for both local and production)
- Docker (for containerized deployments)
- API keys for your LLM provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
  `GEMINI_API_KEY`)

## 1. Local Development

Docker Compose starts both Postgres and the Mash Host with one command.

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: mash
      POSTGRES_USER: mash
      POSTGRES_PASSWORD: mash
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-ONLY", "pg_isready", "-U", "mash"]
      interval: 3s
      retries: 5

  mash:
    build: .
    environment:
      MASH_HOST_APP: pilot.spec:build_pool
      MASH_DATABASE_URL: postgresql://mash:mash@postgres:5432/mash
      MASH_API_KEY: ${MASH_API_KEY:-dev-key}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  pgdata:
```

```bash
docker compose up
```

The Mash Host is accessible at `http://localhost:8000`. To deploy your own
agent instead of Pilot, change `MASH_HOST_APP` to point at your module (e.g.
`my_agent.spec:build_pool`) and make sure the Dockerfile copies your agent
code and installs `mashpy` as a dependency.

```bash
# Connect from another terminal
mash connect --api-base-url http://127.0.0.1:8000 --api-key dev-key --agent pilot
```

## 2. Horizontal Scaling (Multiple Replicas)

Mash Hosts are stateless, since all durable state lives in Postgres. You can run N
identical replicas of the same Host behind a load balancer, all pointing at the
same Postgres instance.

### How It Works

```
                    ┌──────────────────┐
                    │   Load Balancer  │
                    │   (nginx / ALB)  │
                    └────────┬─────────┘
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
   ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
   │  Replica A   │ │  Replica B   │ │  Replica C   │
   │  (all agents)│ │  (all agents)│ │  (all agents)│
   └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
          │                │                │
          └────────────────┼────────────────┘
                           ▼
                ┌──────────────────┐
                │    PostgreSQL    │
                └──────────────────┘
```

Each replica independently runs `build_pool()`, creating its own
`AgentPool` with all agents in-process. Every replica
is a fully self-contained, identical copy.

**Why this works:**

- **Event streaming across replicas.** The runtime event store uses Postgres
  `LISTEN/NOTIFY`. A request can be submitted to Replica A while the SSE
  stream is served from Replica B, because Postgres broadcasts event notifications
  to all listeners.
- **Subagent calls are always in-process.** When the primary agent invokes a
  subagent, it calls the subagent runtime in the same process via
  `InProcessAgentClient`. No cross-replica coordination is needed.
- **Session history is in Postgres.** Conversation memory, traces, and
  interaction state are all persisted. Any replica can serve any session.
- **No sticky sessions required.** The load balancer can use simple
  round-robin routing.

**One caveat:** host compositions defined over the API (`PUT /v1/hosts/{id}`)
are in-memory and per-replica. Code-defined hosts come back on every restart
because `build_pool()` recreates them, but API-defined ones don't, and a `PUT`
only lands on the replica that served it. If your application composes hosts
dynamically, re-`PUT` them on startup (the call is idempotent) or define them
in code. Requests already in flight are unaffected either way — each request
carries a snapshot of its composition.

### docker-compose.yml (Scaled)

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: mash
      POSTGRES_USER: mash
      POSTGRES_PASSWORD: mash
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-ONLY", "pg_isready", "-U", "mash"]
      interval: 3s
      retries: 5

  mash:
    build: .
    environment:
      MASH_HOST_APP: pilot.spec:build_pool
      MASH_DATABASE_URL: postgresql://mash:mash@postgres:5432/mash
      MASH_API_KEY: ${MASH_API_KEY:-dev-key}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    deploy:
      replicas: 3
    depends_on:
      postgres:
        condition: service_healthy

  nginx:
    image: nginx:alpine
    ports:
      - "8000:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - mash

volumes:
  pgdata:
```

### nginx.conf

```nginx
events { worker_connections 1024; }

http {
    upstream mash_hosts {
        server mash:8000;
    }

    server {
        listen 80;

        location / {
            proxy_pass http://mash_hosts;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_buffering off;         # required for SSE
            proxy_read_timeout 3600s;    # long-lived SSE connections
        }
    }
}
```

Scale up or down at any time:

```bash
docker compose up --scale mash=5
```

### Known Limitation: Mid-Request Replica Failure

If a replica dies while executing a request, that request is lost. The DBOS
workflow running the request dies with the process, and no other replica will
pick it up. The request will remain in `REQUEST_ACCEPTED` state indefinitely.

**Impact:** The client's SSE stream will stop receiving events and eventually
time out.

**Mitigation:** Clients should detect timeouts and retry the request. DBOS
deduplication prevents double execution if the original replica recovers.

For most self-hosted deployments, this is an acceptable tradeoff. Replica
failures during active requests are rare, and the simplicity of the current
model (no distributed consensus, no lease management) is a significant
operational advantage.

## 3. Cloud Deployment

For production on AWS, GCP, or Azure, the pattern is the same: stateless Mash
Host containers + managed PostgreSQL.

### Architecture

```
┌──────────────┐     ┌──────────────────────────────┐
│   Internet   │────▶│     Cloud Load Balancer      │
└──────────────┘     └──────────┬───────────────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              ┌──────────┐┌──────────┐┌──────────┐
              │  Host    ││  Host    ││  Host    │
              │  Task    ││  Task    ││  Task    │
              └────┬─────┘└────┬─────┘└────┬─────┘
                   └───────────┼───────────┘
                               ▼
                    ┌──────────────────┐
                    │  Managed Postgres│
                    │  (RDS / CloudSQL)│
                    └──────────────────┘
```

### Recommended Services by Provider

| Component | AWS | GCP | Azure |
|-----------|-----|-----|-------|
| Compute | ECS Fargate / EKS | Cloud Run / GKE | Container Apps / AKS |
| Database | RDS PostgreSQL | Cloud SQL | Azure Database for PostgreSQL |
| Load Balancer | ALB | Cloud Load Balancing | Application Gateway |
| Secrets | Secrets Manager | Secret Manager | Key Vault |

### Quick Deploy with Render

[Render](https://render.com) is the fastest path to production. A single
`render.yaml` Blueprint provisions both the Mash Host and a managed Postgres
database, with no Dockerfile registry or load balancer to configure.

**1. Add a `render.yaml` to your repo root:**

```yaml
services:
  - type: web
    name: my-agent
    runtime: docker
    dockerfilePath: ./Dockerfile
    healthCheckPath: /api/v1/health
    envVars:
      - key: MASH_DATABASE_URL
        fromDatabase:
          name: mash-db
          property: connectionString
      - key: MASH_HOST_APP
        value: my_agent.spec:build_pool
      - key: ANTHROPIC_API_KEY
        sync: false  # prompted at deploy time
      - key: MASH_API_KEY
        sync: false

databases:
  - name: mash-db
    plan: basic-256mb
    databaseName: mash
    user: mash
```

**2. Deploy:**

- Push the repo to GitHub (or GitLab).
- In the Render dashboard, click **New → Blueprint** and select your repo.
- Render detects `render.yaml`, provisions the database, and builds the
  Docker image. You'll be prompted for the `sync: false` env vars
  (`ANTHROPIC_API_KEY`, `MASH_API_KEY`).

**3. Connect:**

```bash
mash connect \
  --api-base-url https://my-agent.onrender.com \
  --api-key <your-mash-api-key> \
  --agent my-agent
```

Render handles TLS, health checks, and zero-downtime deploys out of the box.
To scale horizontally, increase the instance count in the Render dashboard;
no load balancer setup is needed.

### Deployment Steps (Other Providers)

1. **Provision managed PostgreSQL.** Create a Postgres 14+ instance in your
   cloud provider. Note the connection string.

2. **Build and push the Docker image.**
   ```bash
   docker build -t my-registry/mash-pilot:latest .
   docker push my-registry/mash-pilot:latest
   ```

3. **Configure environment variables.** Set these in your container service's
   configuration (not baked into the image):
   ```
   MASH_HOST_APP=pilot.spec:build_pool
   MASH_DATABASE_URL=postgresql://user:pass@db-host:5432/mash
   MASH_API_KEY=<strong-random-key>
   ANTHROPIC_API_KEY=<your-key>
   ```

4. **Deploy container tasks/pods.** Start with 2-3 replicas. Each runs the
   same image with the same environment.

5. **Configure the load balancer.**
   - Target the container port (default `8000`)
   - Health check path: `/api/v1/health`
   - Disable request buffering for SSE support
   - Set idle timeout to ≥ 3600s for long-lived SSE connections
   - No sticky sessions needed

6. **Set up autoscaling.** Scale on CPU utilization or concurrent connection
   count. Each replica handles multiple concurrent requests.

### Connection Pooling

At scale (5+ replicas), use a connection pooler between your Mash replicas and
Postgres. Each replica opens its own connection pool (`psycopg_pool`), and
Postgres has a finite connection limit.

- **AWS:** RDS Proxy
- **GCP:** AlloyDB / Cloud SQL Auth Proxy
- **Self-managed:** PgBouncer (transaction mode)

### Health Checks

The Mash Host exposes `/api/v1/health` which returns agent status and session
information. For container orchestrators, configure:

- **Liveness probe:** `GET /api/v1/health` confirms the process is alive
- **Readiness probe:** `GET /api/v1/health` confirms agents are initialized
  and Postgres is reachable

## 4. Accessing the Host from External Applications

The Mash Host exposes a REST + SSE API under `/api/v1`. Any HTTP client can
interact with it.

### Authentication

If `MASH_API_KEY` is set, include it in requests:

```
Authorization: Bearer <api-key>
X-API-Key: <api-key>          # alternative header
```

### Core API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Health check and agent info |
| `GET` | `/api/v1/agent` | List registered agents |
| `POST` | `/api/v1/agent/{agent_id}/request` | Submit a request |
| `GET` | `/api/v1/agent/{agent_id}/request/{request_id}/events` | Stream response (SSE) |
| `POST` | `/api/v1/agent/{agent_id}/request/{request_id}/interaction` | Respond to an interaction (e.g. AskUser); `interaction_id` goes in the body |

### Example: Submit a Request and Stream the Response

```bash
# 1. Submit a request
REQUEST=$(curl -s -X POST http://localhost:8000/api/v1/agent/pilot/request \
  -H "Authorization: Bearer dev-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "explain the CLI parser", "session_id": "my-session"}')

REQUEST_ID=$(echo $REQUEST | jq -r '.data.request_id')

# 2. Stream the response via SSE
curl -N http://localhost:8000/api/v1/agent/pilot/request/$REQUEST_ID/events \
  -H "Authorization: Bearer dev-key"
```

### Example: Python Client

```python
import httpx

BASE = "http://localhost:8000/api/v1"
HEADERS = {"Authorization": "Bearer dev-key"}

# Submit
resp = httpx.post(
    f"{BASE}/agent/pilot/request",
    headers=HEADERS,
    json={"message": "explain the CLI parser", "session_id": "s1"},
)
request_id = resp.json()["data"]["request_id"]

# Stream SSE
with httpx.stream(
    "GET",
    f"{BASE}/agent/pilot/request/{request_id}/events",
    headers=HEADERS,
    timeout=None,
) as stream:
    for line in stream.iter_lines():
        if line.startswith("data:"):
            print(line)
```

### Example: Connect with the Mash CLI

```bash
mash connect \
  --api-base-url http://your-host:8000 \
  --api-key your-api-key \
  --agent pilot
```

### CORS Configuration

By default, the Host allows requests from `localhost:3000` and
`localhost:5173`. For production, set allowed origins via the `--cors-origin`
CLI flag (repeatable) or configure `cors_allow_origins` in `MashHostConfig`.

## 5. Tearing Down and Restarting

Mash Hosts are designed to be ephemeral. You can tear down and restart
replicas at any time without data loss, because all state is in Postgres.

- **Restart a replica:** The new process calls `build_pool()`, initializes all
  agent runtimes, reconnects to Postgres, and starts serving.
- **Scale to zero:** Stop all replicas. State is preserved in Postgres. Start
  replicas again when needed.
- **Database migrations:** Mash runs pending migrations automatically on
  startup. No manual migration step is required for new deployments. See
  [Schema migrations](#schema-migrations) below for details on how migrations
  work and how to add new ones.

## Schema Migrations

Mash uses an ordered-file migration runner rather than a third-party tool. All Mash tables — the runtime event store, the memory store, and the evals store — share one schema baseline in `src/mash/storage/migrations/`, and every store runs the same runner when it opens. On each run the runner:

1. Takes a Postgres advisory lock, so stores opening concurrently apply each migration exactly once.
2. Creates a `_mash_migrations` tracking table if it does not exist.
3. Reads all `.sql` files from `src/mash/storage/migrations/` in filename order.
4. Applies any file not yet recorded in `_mash_migrations` and records the filename and timestamp so it is not applied again. The whole run is one transaction.

The baseline (`001_baseline.sql`) is idempotent — all `CREATE TABLE` and `CREATE INDEX` statements use `IF NOT EXISTS`, so applying it against an existing database is a no-op.

**Adding a migration:** Create a new file named `NNN_description.sql` (e.g. `002_add_model_column.sql`) in `src/mash/storage/migrations/`. The next startup applies it automatically. Write it to be rollback-safe.

**Upgrading from an earlier deployment:** Releases up to 0.15 tracked runtime and memory migrations separately, in `_mash_migrations` and `_mash_memory_migrations`. The combined baseline applies cleanly on top of a database created by those runners: every statement is guarded, so existing tables and data are untouched, and the old `_mash_memory_migrations` table is simply left behind. `MASH_DATABASE_URL` is required; the agent raises `RuntimeError` at startup if it is unset.

## Environment Variables Reference

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MASH_HOST_APP` | Yes | — | Python module:attribute for `build_pool()` |
| `MASH_DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `MASH_API_KEY` | No | — | API key for authentication |
| `MASH_API_HOST` | No | `127.0.0.1` | Bind host (`0.0.0.0` for containers) |
| `MASH_API_PORT` | No | `8000` | Bind port |
| `MASH_DATA_DIR` | No | `/var/lib/mash` | Persistent data directory |
| `MASH_DISABLE_OBSERVABILITY` | No | — | Set to disable telemetry endpoints |
| `ANTHROPIC_API_KEY` | Provider-dependent | — | Anthropic API key |
| `OPENAI_API_KEY` | Provider-dependent | — | OpenAI API key |
| `GEMINI_API_KEY` | Provider-dependent | — | Google Gemini API key |
| `DBOS_CONDUCTOR_KEY` | No | — | DBOS Conductor key for distributed workflows |

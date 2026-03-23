# mashpy

This repository is the development workspace for Mash.

It contains three main components:

- `mash.runtime`
  The managed runtime for an agent.
- `mash.api`
  The self-hosted API server in `src/mash/api`.
- `mash.cli`
  The bundled CLI in `src/mash/cli`.


## Repo layout

```text
src/mash/                  SDK, host API, and CLI
docs/rfcs/                Protocol and design RFCs
pilot/                     Mash Pilot host built on Mash
tests/                     Unified test suite
Dockerfile                 Base image for Mash host deployments
```

## RFCs

- [Host-to-Agent Protocol (H2A)](docs/rfcs/host-to-agent-protocol.md)

## Mental model

The architecture is:

- app developers use `mashpy` to define one or more `AgentSpec`s
- an app exposes `build_host() -> MashAgentHost`
- `mash.api` loads that host and serves HTTP
- `mash.api` also serves the built-in telemetry UI at `/telemetry`
- `mash` talks to a running Mash API deployment
- deployments are expected to run in a container

Persistence is runtime-level, not app-level.

Mash stores agent state under:

- `<MASH_DATA_DIR>/<agent_id>/state.db`
- `<MASH_DATA_DIR>/<agent_id>/logs/events.jsonl`

If `MASH_DATA_DIR` is not set, the runtime falls back to `/var/lib/mash`.

## Local setup

Create the repo virtualenv, install dependencies, and activate it:

```bash
uv venv
uv sync
source .venv/bin/activate
```

## System Architecture

At a high level, `mashpy` is one distribution with three cooperating surfaces:

- `mash.core`
  The agent loop itself: config, context, provider adapters, and think/act/observe execution.
- `mash.runtime`
  Host-side orchestration: `AgentSpec`, runtime servers, host/client wiring, request streaming, session storage, and subagent delegation.
- `mash.api` and `mash.cli`
  App facing surfaces built on top of the runtime. `mash.api` exposes the FastAPI host service and telemetry UI; `mash.cli` is the remote client.

The normal execution path is:

1. An app defines one or more `AgentSpec`s.
2. `MashAgentHostBuilder` composes those specs into a `MashAgentHost`.
3. `mash.api` starts the host and exposes HTTP endpoints for agent requests, sessions, history, and telemetry.
4. `mash.cli` talks to that HTTP API as a remote client.

```mermaid
flowchart TD
    A["`AgentSpec`"] --> B["`MashAgentHostBuilder`"]
    B --> C["`MashAgentHost`"]
    C --> D["`mash.api`"]
    D --> E["HTTP API"]
    D --> F["Telemetry UI"]
    G["`mash.cli`"] --> E
```

Persistence is runtime-level, not app-level. Each agent stores state under:

- `<MASH_DATA_DIR>/<agent_id>/state.db`
- `<MASH_DATA_DIR>/<agent_id>/logs/events.jsonl`

If `MASH_DATA_DIR` is not set, the runtime falls back to `/var/lib/mash`.

## Agent Runtime

The core execution stack is:

- [src/mash/runtime/spec.py](src/mash/runtime/spec.py)
  `AgentSpec` is the transport-agnostic contract app authors implement.
- [src/mash/runtime/host.py](src/mash/runtime/host.py)
  `MashAgentHost` and `MashAgentHostBuilder` register the primary agent and subagents, start one runtime per agent, and wire host-managed delegation.
- [src/mash/runtime/server.py](src/mash/runtime/server.py)
  `MashAgentServer` owns per-agent execution state, request buffering, session persistence, event logging, and the HTTP request worker.
- [src/mash/core/agent.py](src/mash/core/agent.py)
  `Agent.run()` executes the think/act/observe loop and emits trace + token metadata.
- [src/mash/runtime/client.py](src/mash/runtime/client.py)
  `MashAgentClient` is the runtime-side HTTP client used by the host and by subagent delegation.

```mermaid
sequenceDiagram
    participant User
    participant API as mash.api
    participant Host as MashAgentHost
    participant Client as MashAgentClient
    participant Runtime as MashAgentServer
    participant Agent as mash.core.Agent
    participant Store as State Store

    User->>API: POST /api/v1/agents/{agent_id}/requests
    API->>Host: get_client(agent_id)
    Host-->>API: MashAgentClient
    API->>Client: post_request(...) / stream(...)
    Client->>Runtime: HTTP request + SSE stream
    Runtime->>Agent: process_user_message(...)
    Agent->>Agent: think -> act -> observe
    Agent-->>Runtime: Response + token_usage + trace_id
    Runtime->>Store: save_turn(...)
    Runtime-->>Client: request.accepted / agent.trace / request.completed
    Client-->>API: streamed runtime events
    API-->>User: SSE / final payload

```

Important runtime properties:

- Each `MashAgentServer` is single-flight per server: requests are queued and executed by one worker thread.
- Request lifecycle is streamed over SSE using stable event names:
  `request.accepted`, `request.started`, `agent.trace`, `request.completed`, `request.error`.
- Token accounting is session-scoped inside each runtime. `session_total_tokens` is computed from saved turn metadata and persisted with each turn.
- Runtime logs are written to JSONL and also fanned out as live events for telemetry and remote clients.

## Working on the SDK

The main SDK surface is:

- `AgentSpec` in [src/mash/runtime/spec.py](src/mash/runtime/spec.py)
- `MashAgentHost` in [src/mash/runtime/host.py](src/mash/runtime/host.py)
- `MashAgentClient` in [src/mash/runtime/client.py](src/mash/runtime/client.py)
- `MashAgentServer` in [src/mash/runtime/server.py](src/mash/runtime/server.py)

The intended app shape is:

```python
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.runtime import AgentSpec, MashAgentHostBuilder
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry


class PrimaryAgent(AgentSpec):
    def get_agent_id(self) -> str:
        return "primary"

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="primary", api_key="...")

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="primary", system_prompt="You are helpful.")


def build_host():
    return MashAgentHostBuilder().primary(PrimaryAgent()).build()
```

## Subagent Invocation Flow

Subagent delegation is host-managed, not a special local shortcut.

1. The host registers the primary agent and subagents in [src/mash/runtime/host.py](src/mash/runtime/host.py).
2. On startup, the host injects subagent routing guidance into the primary system prompt and registers `InvokeSubagent`.
3. `InvokeSubagentTool` in [src/mash/tools/subagent.py](src/mash/tools/subagent.py) resolves the target subagent client and submits a normal streamed request.
4. The subagent request runs through that subagent’s own `MashAgentServer`, persistence layer, and session namespace.
5. Streamed request events are forwarded back to the primary runtime as `subagent.*` trace events for observability.

```mermaid
sequenceDiagram
    participant Primary as Primary Agent
    participant Tool as InvokeSubagentTool
    participant Host as MashAgentHost
    participant Client as MashAgentClient
    participant Server as Subagent MashAgentServer
    participant Subagent as Subagent mash.core.Agent

    Primary->>Tool: InvokeSubagent(agent_id, prompt, opts)
    Tool->>Host: get_client(agent_id)
    Host-->>Tool: MashAgentClient
    Tool->>Client: post_request(prompt, session_id=subagent_session_id)
    Client->>Server: HTTP request + SSE stream
    Server->>Subagent: process_user_message(...)
    Subagent->>Subagent: think -> act -> observe
    Subagent-->>Server: Response + token_usage + trace_id
    Server-->>Client: request.accepted / agent.trace / request.completed
    Client-->>Tool: streamed events
    Tool-->>Primary: ToolResult(text + metadata)
```

Relevant implementation details:

- Subagent session ids are deterministic via [src/mash/runtime/session.py](src/mash/runtime/session.py) using:
  `primary_app_id + primary_session_id + subagent_id`.
- The subagent keeps its own session history and token totals.
- The primary agent only owns its own turns and token usage; subagent execution is correlated, but not merged into the primary session’s token total.

## Core Modules

When working on `mashpy`, these are the main files to orient around:

- [src/mash/core/agent.py](src/mash/core/agent.py)
  Core think/act/observe loop, tool execution, token aggregation, and trace emission.
- [src/mash/runtime/server.py](src/mash/runtime/server.py)
  Session persistence, request queueing, SSE event buffering, and runtime event fan-out.
- [src/mash/runtime/host.py](src/mash/runtime/host.py)
  Multi-agent composition and subagent tool wiring.
- [src/mash/api/app.py](src/mash/api/app.py)
  FastAPI composition for the public host API, telemetry endpoints, and auth.
- [src/mash/cli/main.py](src/mash/cli/main.py)
  Unified `mash` CLI entrypoint for remote operations and `mash host serve`.
- [src/mash/cli/shell.py](src/mash/cli/shell.py)
  Remote REPL path, including streamed request handling and chain rendering.

Common contributor questions:

- If you are changing request or event shapes, update runtime tests, API tests, and CLI streaming behavior together.
- If you are changing token accounting or persistence, validate both `tests/mash/runtime/test_engine.py` and `tests/mash/runtime/test_host_integration.py`.
- If you are changing telemetry behavior, remember there are two layers:
  the API routes in `mash.api`, and the bundled frontend assets under `src/mash/api/web`.

## Mash Pilot

[pilot/spec.py](pilot/spec.py) is the main in-repo agent app built on Mash.

Pilot uses standard Mash building blocks:

- `PilotSpec` is the primary `AgentSpec`.
- `build_host()` composes the primary pilot plus module-specific copilots with `MashAgentHostBuilder`.
- `mash.api` serves that host over HTTP.
- `mash.cli` connects to it as a remote client.

The Pilot host currently registers:

- `pilot`: the primary codebase guide for shared Mash modules.
- `cli-copilot`: specialist for `src/mash/cli`.
- `api-copilot`: specialist for `src/mash/api`.
- `mcp-copilot`: specialist for `src/mash/mcp`.

Run Pilot from the activated repo environment:

```bash
export OPENAI_API_KEY=...
export MASH_DATA_DIR=.mash

python -m pilot.spec \
  --workspace-root /Users/sid/Projects/mashpy \
  --host 127.0.0.1 \
  --port 8000 \
  --api-key secret
```

Connect with the bundled CLI:

```bash
mash connect --api-base-url http://127.0.0.1:8000 --api-key secret --agent pilot
mash status
mash agents
mash repl
```

Open the built-in telemetry UI:

- [http://127.0.0.1:8000/telemetry](http://127.0.0.1:8000/telemetry)

Pilot is useful as both:

- a real Mash app built on the same `AgentSpec` and `MashAgentHostBuilder` contracts exposed to users
- the canonical reference for how to build a multi-agent Mash host with specialized subagents

## Masher

Masher is another agent built on Mash, implemented in [src/mash/agents/masher/spec.py](src/mash/agents/masher/spec.py).

It is a built-in log-analysis specialist that uses the normal Mash runtime contracts:

- `MasherAgentSpec` is a regular `AgentSpec`.
- it uses Mash memory/store tools to resolve recent sessions and traces
- it reads structured JSONL event logs
- it can append normalized online-eval rows with `append_jsonl`

Pilot enables Masher by default with `.enable_masher()`, but Masher can also be registered in any other Mash host that wants a log-analysis subagent.

## Running the host API directly

You can start the API server either from Python or through `mash host serve`.

From Python:

```python
from mash.api import MashHostConfig, run_host

from my_app import build_host

run_host(build_host(), config=MashHostConfig(bind_host="0.0.0.0", bind_port=8000))
```

From the CLI:

```bash
MASH_HOST_APP=my_app:build_host \
MASH_API_HOST=0.0.0.0 \
MASH_API_PORT=8000 \
MASH_DATA_DIR=/var/lib/mash \
mash host serve
```

For containerized deployments, the env-driven startup path is the intended one.

## Docker workflow

The root [Dockerfile](Dockerfile) is the base image for Mash host deployments.

Build it:

```bash
docker build -t mashpy/mash-host-base:latest .
```

The Pilot image is defined in [pilot/Dockerfile](pilot/Dockerfile).

Build and run it:

```bash
docker build -t mashpy/pilot:latest -f pilot/Dockerfile .
docker run \
  -p 8000:8000 \
  -e OPENAI_API_KEY=... \
  -e MASH_HOST_APP=pilot.spec:build_host \
  -e MASH_API_KEY=secret \
  -e MASH_DATA_DIR=/var/lib/mash \
  -v $(pwd)/data:/var/lib/mash \
  mashpy/pilot:latest
```

The container contract is:

- `MASH_HOST_APP` points at `module:build_host`
- `MASH_DATA_DIR` points at the persistent state root
- port `8000` is exposed by default
- operators mount persistent storage at `/var/lib/mash`

## Tests

Focused runtime and API tests:

```bash
PYTHONPATH=src \
pytest -q \
  tests/mash/runtime/test_engine.py \
  tests/mash/runtime/test_host_integration.py \
  tests/mash/api/test_host_server.py
```

CLI-focused tests:

```bash
PYTHONPATH=src \
pytest -q \
  tests/mash/cli/test_main.py \
  tests/mash/cli/test_shell.py
```

When changing cross-surface behavior, set `PYTHONPATH=src` so tests resolve the unified workspace sources instead of stale installed copies.

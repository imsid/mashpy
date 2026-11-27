# Mash Package

`src/mash` is the main library package for Mash, a self-hosted, durable runtime for code-authored automations. It holds the SDK/runtime contracts for agents and workflows plus the hosted surfaces that make them usable in practice.

## What Lives Here
- `core`: single-agent execution loop, config, context, and LLM provider contracts.
- `runtime`: agent specs, host composition, server/client runtime behavior, and session routing.
- `workflows`: code-defined host-level workflow specs, registry, engine, and service.
- `evals`: synthetic eval data model (datasets, rubrics, experiments, runs), scoring service, per-row operational metrics, and the Postgres eval store.
- `api`: FastAPI app wiring, host serving helpers, and admin dashboard UI serving.
- `policies`: the `Policy` model (sandbox, browser sites, wait cap) and the `PolicyStore` over the `policies` table.
- `sessions`: sessions and events: the `EventStore` over the `sessions` and `events` tables, and the host's `EventService`, which queues each session's events, runs them one at a time through the companion and the request it routes to, and records their outcomes.
- `cli`: the `mash` command-line, remote shell flow, and terminal rendering.
- `tools`: built-in tools such as the sandbox tools (`run_code`, `edit_file`, `sandbox_put`, `sandbox_get`), the browser tools, MCP-backed tools, the board tools (`read_board`, `search_board`) and `RunSubAgents`.
- `sandbox`: per-policy sandboxes where agents' code runs: the daemon inside each one, the Docker provider, and the host's `SandboxManager`.
- `browser`: per-policy browsers that agents listing sites drive: the daemon inside each one (Chromium, Playwright MCP and the live view), the Docker provider, and the host's `BrowserManager`.
- `skills`: optional skill abstractions and registration.
- `memory`: persistent storage, retrieval, and compaction support.
- `logging`: structured JSONL events and trace correlation helpers.
- `mcp`: Model Context Protocol client/server/manager integration.
- `agents`: built-in agents and workflows: masher (`EvalAgentSpec`, eval workflows) and Cortex (per-app memory, `cortex-agent`, `cortex-build`, the Cortex API).

## How The Pieces Fit Together
1. A user implements `mash.runtime.AgentSpec` to define one agent.
2. `mash.runtime.HostBuilder` registers agents and code-defined workflows into a `Pool`, with optional `Host` compositions. `enable_evals()` adds Masher and its workflows. A pool with no user agents is a valid, workflow-only deploy.
3. `mash.workflows.WorkflowService` runs workflows as ordered step pipelines; agent steps send normal Mash requests to registered or internal workflow agents.
4. Each agent runs as a subprocess of the host that serves its runtime with `mash.runtime.AgentServer` over HTTP + SSE on a localhost port. The pool starts them through an `AgentProvider` (`AgentProcessProvider`) and keeps one `AgentClient` per agent. Agents can be enabled and disabled on a running host.
5. Every request acts for an app under a policy. The policy owns a sandbox, where the code an agent's model writes runs, reached through the host's `/sandbox` relay, and people open what it made at links the host serves under `/s/`; a host without a Docker daemon runs and its agents get no sandbox tools. An agent whose spec lists sites drives the policy's browser through the host's `/browser` relay, and hands it to a person at a live-view link when a site needs a sign-in. The policy also narrows the sandbox and browser tools and caps how long an interaction waits.
6. `mash.api` exposes that host over HTTP, including workflow routes, and `mash.cli` talks to it as a remote client.
7. Every input to a host is an event in a session (`mash.sessions`). Events in a session run one at a time, in arrival order, each starting as a request on the built-in companion. Its classifier picks one of the host's agents or workflows; the companion starts that agent's request in the session, through the host's `/h2a` relay, or runs the workflow. Every agent in a conversation reads the same board: the session's entries, one per request, in order. The others wait in the session's queue, kept in Postgres with the ledger of every event's outcome.
8. Policies (`mash.policies`) are named and durable, created over the API; a session binds to one with its first event.
9. A pool that calls `HostBuilder.enable_cortex()` keeps per-app memory: each request reads its app's Cortex, and the `cortex-build` workflow builds it on demand.

## Public Entry Points
- `mash`: top-level authoring exports (`AgentSpec`, `AgentMetadata`, `Host`, `HostBuilder`, `Pool`, `WorkflowSpec`, `CodeStep`, `AgentStep`, `StepContext`).
- `mash.runtime.AgentSpec`: single-agent build contract.
- `mash.runtime.HostBuilder`: pool composition entrypoint.
- `mash.workflows.WorkflowSpec` and `mash.workflows.WorkflowService`: code-defined workflow definitions and orchestration.
- `mash.api.create_app` and `mash.api.run_host`: hosted HTTP surface.
- `mash.cli`: CLI client, shell, and command helpers for remote operation.

## Package Boundaries
- Keep SDK/runtime primitives in `core` and `runtime`.
- Keep host-level workflow execution and storage in `workflows`.
- Keep HTTP-specific behavior in `api` and terminal UX in `cli`.
- Keep tool schemas centralized in `tools`.
- Keep logs machine-readable and stable enough for downstream inspection.

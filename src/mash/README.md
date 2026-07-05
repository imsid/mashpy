# Mash Package

`src/mash` is the main library package for Mash. It holds the SDK/runtime contract for agents plus the hosted surfaces that make those agents usable in practice.

## What Lives Here
- `core`: single-agent execution loop, config, context, and LLM provider contracts.
- `runtime`: agent specs, host composition, server/client runtime behavior, and session routing.
- `workflows`: code-defined host-level workflow specs, registry, and DBOS-backed orchestration service.
- `evals`: synthetic eval data model (datasets, rubrics, experiments, runs), scoring service, per-row operational metrics, and the Postgres eval store.
- `api`: FastAPI app wiring, host serving helpers, and admin dashboard UI serving.
- `cli`: the `mash` command-line, remote shell flow, and terminal rendering.
- `tools`: built-in tools such as bash access, MCP-backed tools, and subagent invocation.
- `skills`: optional skill abstractions and registration.
- `memory`: persistent storage, retrieval, and compaction support.
- `logging`: structured JSONL events and trace correlation helpers.
- `mcp`: Model Context Protocol client/server/manager integration.
- `agents`: built-in specialist agent specs such as the workflow-only `MasherAgentSpec`.

## How The Pieces Fit Together
1. A user implements `mash.runtime.AgentSpec` to define one agent.
2. `mash.runtime.HostBuilder` composes one primary agent, optional subagents, optional workflow-only agents, and optional code-defined workflows into a host.
3. `mash.workflows.WorkflowService` can orchestrate ordered task chains by sending normal Mash requests to registered or internal workflow agents.
4. `mash.runtime.AgentServer` exposes each per-agent runtime over HTTP + SSE.
5. `mash.api` exposes that host over HTTP, including workflow routes, and `mash.cli` talks to it as a remote client.
6. Primary agents can delegate focused work through the runtime host and `InvokeSubagent`.

## Public Entry Points
- `mash.runtime.AgentSpec`: single-agent build contract.
- `mash.runtime.HostBuilder`: host composition entrypoint.
- `mash.workflows.WorkflowSpec` and `mash.workflows.WorkflowService`: code-defined workflow definitions and orchestration.
- `mash.api.create_app` and `mash.api.run_host`: hosted HTTP surface.
- `mash.cli`: CLI client, shell, and command helpers for remote operation.

## Package Boundaries
- Keep SDK/runtime primitives in `core` and `runtime`.
- Keep host-level workflow orchestration and DBOS workflow contracts in `workflows`.
- Keep HTTP-specific behavior in `api` and terminal UX in `cli`.
- Keep tool schemas centralized in `tools`.
- Keep logs machine-readable and stable enough for downstream inspection.

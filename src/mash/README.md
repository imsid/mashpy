# Mash Package

`src/mash` is the main library package for Mash. It holds the SDK/runtime contract for agents plus the hosted surfaces that make those agents usable in practice.

## What Lives Here
- `core`: single-agent execution loop, config, context, and LLM provider contracts.
- `runtime`: agent specs, host composition, server/client runtime behavior, and session routing.
- `api`: FastAPI app wiring, host serving helpers, and telemetry UI serving.
- `cli`: the `mash` command-line, remote shell flow, and terminal rendering.
- `tools`: built-in tools such as bash access, MCP-backed tools, and subagent invocation.
- `skills`: optional skill abstractions and registration.
- `memory`: persistent storage, retrieval, and compaction support.
- `logging`: structured JSONL events and trace correlation helpers.
- `mcp`: Model Context Protocol client/server/manager integration.
- `agents`: built-in specialist agent specs such as `MasherAgentSpec`.

## How The Pieces Fit Together
1. A user implements `mash.runtime.AgentSpec` to define one agent.
2. `mash.runtime.HostBuilder` composes one primary agent and optional subagents into a host.
3. `mash.runtime.AgentServer` exposes each per-agent runtime over HTTP + SSE.
4. `mash.api` exposes that host over HTTP, and `mash.cli` talks to it as a remote client.
5. Primary agents can delegate focused work through the runtime host and `InvokeSubagent`.

## Public Entry Points
- `mash.runtime.AgentSpec`: single-agent build contract.
- `mash.runtime.HostBuilder`: host composition entrypoint.
- `mash.api.create_app` and `mash.api.run_host`: hosted HTTP surface.
- `mash.cli`: CLI client, shell, and command helpers for remote operation.

## Package Boundaries
- Keep SDK/runtime primitives in `core` and `runtime`.
- Keep HTTP-specific behavior in `api` and terminal UX in `cli`.
- Keep tool schemas centralized in `tools`.
- Keep logs machine-readable and stable enough for downstream inspection.

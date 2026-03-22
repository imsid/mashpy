# Runtime

`src/mash/runtime` contains the hosted execution layer that turns an `AgentSpec` into a running Mash deployment.

## What This Package Does
- Defines the `AgentSpec` contract used to build agents.
- Composes one primary agent and optional subagents into a `MashAgentHost`.
- Runs per-agent servers that wire together core execution, tools, memory, logs, and MCP integrations.
- Provides runtime client/session contracts used by API and CLI surfaces.

## Main Components
- `spec.py`: `AgentSpec`, the single-agent SDK contract.
- `server.py`: `MashAgentServer`, the execution entrypoint for one agent.
- `host.py`: host composition, startup, and agent registry behavior.
- `client.py`: runtime client helpers for hosted agents.
- `session.py`: session and deterministic subagent session ID derivation.
- `types.py`: runtime result types and `SubAgentMetadata`.
- `http.py`: transport helpers used by the hosted runtime.
- `errors.py`: runtime-facing exception types.

## Typical Flow
1. A caller implements `AgentSpec`.
2. The runtime builds tool, skill, memory, logging, and model dependencies from that spec.
3. `MashAgentHostBuilder` composes the primary agent and optional subagents into one host.
4. The API layer exposes that host, and the CLI or other clients call it through the runtime contracts defined here.

## Important Invariants
- `AgentSpec` is the build boundary for a single agent.
- Subagent session derivation must remain deterministic.
- Runtime control and session behavior need to stay compatible with both `mash.api` and `mash.cli`.

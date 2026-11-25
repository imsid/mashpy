# Agents

`src/mash/agents` contains built-in agent specs that ship with Mash.

## What This Package Does
- Houses built-in specialists implemented on top of the normal Mash runtime APIs.
- Exposes those specs from `__init__.py` so host builders can register them directly.
- Keeps built-in agent prompts, tools, and module-specific behavior separate from generic runtime code.

## Current Built-In Agents
- `masher`: bundled workflow module containing `EvalAgentSpec` and two Masher
  eval workflows, registered by `HostBuilder.enable_evals()`. See
  [`masher/README.md`](masher/README.md).

- `companion`: the built-in router `CompanionSpec`, registered by
  every pool. Every event posted to a host runs on it, and it routes each
  message among the host's agents and workflows. After reading a
  `RunSubAgents` result it forwards one agent's answer unchanged with
  `ForwardAnswer`, reruns a session with a corrected brief, or writes its own
  reply. It replays its turns as replies (`AgentConfig.replay="replies"`):
  each user message, the routes behind the reply, and the reply. Its
  `logs.py` holds `ListAgentTraces`, `InspectAgentTrace` (`view` `summary`,
  `transcript` or `events`) and `GetAgentTurns`, which read an agent's traces
  and turns by agent id with a session or trace id, only for the companion's
  own children in the current session. They return whole results; a long one
  is previewed like any tool result and read on with `ReadToolResult`.

## Package Boundary
- Built-in agents should compose `mash.runtime`, `mash.tools`, `mash.memory`, and `mash.logging`.
- They should not reimplement generic runtime primitives inside this package.

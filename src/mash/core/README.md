# Core

`src/mash/core` contains the single-agent execution primitives used by every hosted or local Mash agent.

## What This Package Does
- Defines the agent loop that sends model requests, executes tool calls, and produces final responses.
- Owns `AgentConfig`, which is the main contract for system prompt blocks, limits, and runtime-facing behavior.
- Defines execution context types shared during agent turns.
- Provides the LLM provider abstraction plus concrete provider adapters under `llm/`.

## Main Components
- `agent.py`: the think-act-observe loop and tool-call orchestration.
- `config.py`: `AgentConfig` and model/runtime execution settings.
- `context.py`: context objects shared while a turn is in progress.
- `llm/base.py`: provider interface.
- `llm/openai.py` and `llm/anthropic.py`: provider integrations.
- `llm/types.py`: normalized request/response structures.

## Role In The System
- `runtime` builds on `core` to host agents over transport boundaries.
- `api` and `cli` should consume behavior that is already expressed through `core` and `runtime`, not reimplement it.
- Built-in agents depend on this package for execution semantics and provider wiring.

## Boundaries
- Keep `core` generic and reusable.
- Do not move HTTP, terminal UX, or host-composition policy into this package.
- Preserve tool-call metadata and provider request/response shapes expected by downstream runtime and logging code.

# Agents

`src/mash/agents` contains built-in agent specs that ship with Mash.

## What This Package Does
- Houses built-in specialists implemented on top of the normal Mash runtime APIs.
- Exposes those specs from `__init__.py` so host builders can register them directly.
- Keeps built-in agent prompts, tools, and module-specific behavior separate from generic runtime code.

## Current Built-In Agents
- `masher`: bundled workflow module containing `EvalAgentSpec` and four Masher
  workflows. The eval agent is registered visibly in every pool. See
  [`masher/README.md`](masher/README.md).

## Package Boundary
- Built-in agents should compose `mash.runtime`, `mash.tools`, `mash.memory`, and `mash.logging`.
- They should not reimplement generic runtime primitives inside this package.

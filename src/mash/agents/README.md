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

- `companion`: the built-in router `CompanionSpec`, registered by every
  pool. Every event posted to a host starts on it. It runs no agent loop: its
  request classifies the message with one structured output call over an enum
  of the host's agent ids, workflow ids, `about` and `unclear`
  (`classifier.py` holds the prompts, schemas and the text it answers with),
  then starts the chosen agent's request in the conversation's session, runs
  the chosen workflow, answers `about` from the host's descriptions or asks
  the user to pick an agent. The flow itself is `mash.runtime.engine.route`.
  Its `AgentConfig.compaction_token_threshold` (100k) is the board's: it
  writes a conversation's board summary with its own model once the last
  entry's `board_tokens` reaches it. `COMPANION_AGENT_<PROVIDER>_MODEL` sets
  the classifier's model.

## Package Boundary
- Built-in agents should compose `mash.runtime`, `mash.tools`, `mash.memory`, and `mash.logging`.
- They should not reimplement generic runtime primitives inside this package.

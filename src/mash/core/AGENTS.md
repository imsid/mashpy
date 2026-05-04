# AGENTS Guide for `src/mash/core`

## What Must Stay True
- `Agent` remains the core single-agent execution loop.
- `AgentConfig` is the primary configuration contract for agent behavior.
- LLM provider interfaces and provider-specific integrations stay under `core.llm`.
- Core types remain reusable across local runtimes, hosted APIs, and built-in agents.

## Change Rules
- Keep `core` generic; do not mix in API, CLI, or subagent-host policy.
- Preserve tool-call schema behavior and response metadata relied on by downstream runtime code.
- If core execution defaults change, update downstream docs and tests that depend on that behavior.

## Minimal Validation
- `python -m compileall src/mash/core`
- Verify one submit-then-stream request path and one tool-call path.

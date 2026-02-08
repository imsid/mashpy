# AGENTS Guide for `src/mash/core`

## Scope
Core agent runtime: config, context data model, LLM provider adapter, and execution loop.

## Invariants
- `Agent.run()` must always clear trace context (`clear_trace_id`) in `finally`.
- `Agent` response metadata should include `trace_id` and run-level token usage (`input`, `output`).
- `think()` is the only place that calls the LLM; `act()` only executes tools; `observe()` only mutates context with results.
- LLM response parsing must preserve Anthropic block semantics (`text`, `tool_use`, server/tool-result blocks).
- Tool call validation should reject missing required args before execution.

## Extension Points
- Add config flags in `AgentConfig` and validate in `__post_init__`.
- New model/provider support should implement `LLMProvider` and keep `parse_response()` contract:
  `(assistant_text, tool_calls, content_blocks)`.
- Keep tool-search behavior isolated in `_build_tool_definitions()` and `_get_betas()`.

## Guardrails
- Do not mix UI/renderer formatting logic into runtime decisions.
- Keep stop-reason handling explicit (`end_turn` vs `max_tokens`).
- Preserve structured debug events when changing token estimation or tool def generation.

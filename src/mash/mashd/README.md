## mashd

`mashd` is the LLM runtime module for Mash. It contains the agent loop,
LLM provider adapters, telemetry helpers, and shared dataclasses used by
agent workflows.

### Components

- `agent.py` - `AgentRuntime` orchestration and tool execution.
- `models.py` - dataclasses like `AgentConfig`, `AgentReply`, and loop context.
- `llm_provider.py` - `LLMProvider` abstraction + `AnthropicProvider`.
- `telemetry.py` - `TokenUsage` and `TelemetryCollector`.
- `runtime_tools.py` - runtime-provided tools such as memory helpers.
- `bash_session.py` - Claude bash tool session management.
- `tools.py` - tool registry, tool specs, and invocation helpers.

### Notes

- The top-level `mash` package re-exports the public agent/telemetry types.
- Use `mashd` directly for internal integrations or advanced customization.

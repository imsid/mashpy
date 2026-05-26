# LLM

`src/mash/core/llm` contains the provider-neutral LLM contract used by Mash plus the concrete provider adapters currently shipped with the project.

## What This Package Exposes
- `LLMProvider`: abstract provider protocol used by the rest of the runtime
- `BaseLLMProvider`: shared logging/trace plumbing for concrete adapters
- `LLMRequest`, `LLMResponse`: normalized request/response models
- `LLMMessage`, `LLMContentBlock`: normalized conversation content types
- `LLMToolDefinition`: normalized tool schema passed to providers
- `LLMTokenUsage`: normalized usage accounting
- `LLMCapabilities`: optional provider capability flags
- `AnthropicProvider`
- `OpenAIProvider`

## Providers Available

### `AnthropicProvider`
- Provider name: `anthropic`
- Default model: `DEFAULT_ANTHROPIC_MODEL`
- Default env source: `ANTHROPIC_MODEL`, fallback `claude-haiku-4-5-20251001`
- API key source:
  - explicit `api_key`
  - otherwise `ANTHROPIC_API_KEY`

Behavior:
- Uses the Anthropic Messages API.
- Requires a Claude model name.
- Supports prompt-caching annotations on system/tool blocks.
- Supports provider beta flags via `request.provider_options["betas"]`.
- Translates `request.provider_options["structured_output"]` into the
  Messages API `output_config` json_schema format.
- Returns capability flags:
  - `beta_flags=True`
  - `server_tools=True`

### `OpenAIProvider`
- Provider name: `openai`
- Default model: `DEFAULT_OPENAI_MODEL`
- Default env source: `OPENAI_MODEL`, fallback `gpt-5-mini`
- API key source:
  - explicit `api_key`
  - otherwise `OPENAI_API_KEY`

Behavior:
- Uses the OpenAI Responses API.
- Rejects Anthropic/Claude model names.
- Supports reasoning-oriented provider options through `request.provider_options`.
- Supports prompt caching through `prompt_cache_key` / `prompt_cache_retention`.
- Temperature is omitted for `gpt-5*` models.
- Translates `request.provider_options["structured_output"]` into the
  Responses API `text.format` json_schema entry; honors
  `request.provider_options["structured_output_strict"]` (default `True`).
- Returns capability flags:
  - `reasoning_controls=True`

## `LLMProvider` Protocol

The runtime interacts with providers through the abstract interface in [base.py](/Users/sid/Projects/mashpy/src/mash/core/llm/base.py).

Required members:

`model -> str`
- Returns the provider-owned model identifier actually used by the adapter.

`send(request: LLMRequest) -> LLMResponse`
- Accepts a normalized request.
- Returns a normalized response regardless of provider-specific wire format.

`set_event_logger(logger, session_id, app_id) -> None`
- Binds structured LLM logging to the provider.
- Used so provider calls emit `llm.request.start`, `llm.request.complete`, and `llm.request.error`.

`set_trace_id(trace_id) -> None`
- Binds the current agent trace id so provider events can be correlated with agent execution.

Optional members:

`get_event_logger_session_id() -> str | None`
- Returns the currently bound logging session if the provider tracks it.

`capabilities() -> LLMCapabilities`
- Returns optional feature flags beyond the core contract.
- Default implementation returns all-false capabilities.

## Normalized Request Contract

The runtime sends providers an `LLMRequest` with these fields:

```python
LLMRequest(
    model: str,
    system: SystemPrompt,
    messages: list[LLMMessage],
    tools: list[LLMToolDefinition],
    max_tokens: int,
    temperature: float = 1.0,
    use_prompt_caching: bool = True,
    provider_options: dict[str, Any] = {},
)
```

Field notes:
- `model`: requested model identifier
- `system`: system prompt in the runtime’s normalized `SystemPrompt` form
- `messages`: normalized transcript
- `tools`: normalized callable tool definitions
- `max_tokens`: requested output cap
- `temperature`: provider sampling control when supported
- `use_prompt_caching`: hint for providers that support caching
- `provider_options`: adapter-specific escape hatch for provider-native options

## Normalized Message And Content Shapes

`LLMMessage`
- `role`
- `content`
- `tool_call_id`
- `metadata`

`LLMContentBlock`
- `type`
- `data`

Supported normalized block constructors:
- `LLMContentBlock.text(...)`
- `LLMContentBlock.tool_call(...)`
- `LLMContentBlock.tool_result(...)`

Important block types used by the runtime:
- `text`
- `tool_call`
- `tool_result`

`coerce_content_blocks(...)`
- Converts stored/raw block payloads into normalized runtime blocks.
- Normalizes legacy/provider-native forms such as:
  - `tool_use` -> `tool_call`
  - provider tool-result variants -> `tool_result`

## Tool Definition Contract

Providers receive tools as `LLMToolDefinition`:

```python
LLMToolDefinition(
    name: str,
    description: str,
    parameters_json_schema: dict[str, Any],
    metadata: dict[str, Any] = {},
)
```

Notes:
- `parameters_json_schema` is the provider-neutral tool input schema.
- `metadata` is passed through to the concrete adapter and may contain provider-specific hints.
- `to_debug_dict()` returns a flattened debug-friendly representation used elsewhere in the runtime.

## Normalized Response Contract

Providers must return `LLMResponse`:

```python
LLMResponse(
    text: str,
    tool_calls: list[Any],
    content_blocks: list[LLMContentBlock],
    stop_reason: str | None = None,
    usage: LLMTokenUsage | None = None,
    provider_response: Any = None,
    provider_metadata: dict[str, Any] = {},
)
```

Field notes:
- `text`: plain assistant text extracted from the provider response
- `tool_calls`: parsed tool-call objects for the agent loop
- `content_blocks`: normalized content blocks preserving richer structure
- `stop_reason`: normalized stop reason such as `end_turn`, `tool_call`, `max_tokens`, or `error`
- `usage`: normalized token accounting if the provider returns it
- `provider_response`: raw provider SDK response for debugging/introspection
- `provider_metadata`: extra normalized provider metadata not modeled elsewhere

## Token Usage Contract

`LLMTokenUsage`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `cache_read_tokens`
- `cache_write_tokens`
- `metadata`

Providers should map their native token accounting into these fields when available.

## Capability Flags

`LLMCapabilities` currently exposes:
- `beta_flags`
- `reasoning_controls`
- `server_tools`
- `streaming`

Current provider capability summary:
- `AnthropicProvider`: beta flags and server tools
- `OpenAIProvider`: reasoning controls
- Neither adapter currently advertises `streaming=True`

## Provider Option Notes

`provider_options` is intentionally adapter-specific.

Current known usage:
- Anthropic:
  - `betas`: list of Anthropic beta flags
  - `structured_output`: JSON-schema dict (see Structured Output below)
- OpenAI:
  - `prompt_cache_key`
  - `prompt_cache_retention`
  - `structured_output`: JSON-schema dict (see Structured Output below)
  - `structured_output_strict`: bool, defaults to `True`
  - any additional Responses API params not filtered out by the adapter

Callers should treat `provider_options` as an escape hatch, not a stable cross-provider contract.

## Structured Output

The runtime drives structured output by passing a JSON schema through
`LLMRequest.provider_options["structured_output"]`. Both shipped adapters
detect this key and translate it into their provider-native schema-format
argument, then strip it (and `structured_output_strict` on OpenAI) from the
generic `provider_options` passthrough so it does not appear twice. The
literal user/assistant instruction that asks the model to produce the
structured payload is added by the runtime
([`finalize_structured_output`](../../runtime/engine/steps.py)), not by the
providers.

### `AnthropicProvider`

When `provider_options["structured_output"]` is a dict, the adapter sets the
Messages API `output_config`:

```python
{
    "format": {
        "type": "json_schema",
        "schema": <user-provided schema dict>,
    }
}
```

No additional flags are required.

### `OpenAIProvider`

When `provider_options["structured_output"]` is a dict, the adapter sets the
Responses API `text` field:

```python
{
    "format": {
        "type": "json_schema",
        "name": <derived>,
        "schema": <user-provided schema dict>,
        "strict": <bool>,
    }
}
```

- `name` is derived from the schema's `title` field by sanitizing
  non-alphanumeric characters (fallback: `StructuredOutput`).
- `strict` defaults to `True` and can be overridden by
  `provider_options["structured_output_strict"]`.

For the developer-facing entry point (Pydantic models, request submission,
response shape, HTTP API), see
[`src/mash/runtime/README.md`](../../runtime/README.md) (Structured Output).

## Logging Behavior

Concrete providers built on `BaseLLMProvider` automatically emit:
- `llm.request.start`
- `llm.request.complete`
- `llm.request.error`

Logged fields include:
- provider name
- model
- duration
- token counts
- trace id
- tool names
- beta flags when applicable

## Source Of Truth
- Provider contract: [base.py](/Users/sid/Projects/mashpy/src/mash/core/llm/base.py)
- Normalized models: [types.py](/Users/sid/Projects/mashpy/src/mash/core/llm/types.py)
- Anthropic adapter: [anthropic.py](/Users/sid/Projects/mashpy/src/mash/core/llm/anthropic.py)
- OpenAI adapter: [openai.py](/Users/sid/Projects/mashpy/src/mash/core/llm/openai.py)

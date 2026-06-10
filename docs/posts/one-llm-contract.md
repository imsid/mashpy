---
title: One LLM Contract, Three Providers
description: The normalized request/response contract every Mash agent speaks, and how the Anthropic, OpenAI, and Gemini adapters translate it — caching, streaming, and structured output included.
date: 2026-06-10
author: imsid
tags:
  - internals
  - llm
---

# One LLM Contract, Three Providers

Each agent in a Mash host picks its own model in `build_llm()` — a cheap one for triage, a capable one for the primary, different vendors in the same process. The runtime supports this by keeping provider SDKs behind the adapter layer: everything above it speaks exactly two types, `LLMRequest` in and `LLMResponse` out.

```python
# src/mash/core/llm/types.py — what the runtime sends
LLMRequest(
    model: str,
    system: SystemPrompt,
    messages: list[LLMMessage],
    tools: list[LLMToolDefinition],
    max_tokens: int,
    temperature: float = 1.0,
    use_prompt_caching: bool = True,
    streaming: bool = False,
    provider_options: dict[str, Any] = {},
)
```

The response side mirrors it: `text`, parsed `tool_calls`, normalized `content_blocks`, a normalized `stop_reason`, and `LLMTokenUsage` with cache read/write counts. The raw SDK response is preserved in `provider_response` for debugging. This post is about what the adapters do to honor that contract, because the three providers behind it disagree about almost everything.

## Three wire formats, one translation layer

```mermaid
flowchart LR
    R["LLMRequest\n(normalized)"] --> A["AnthropicProvider\n→ Messages API"]
    R --> O["OpenAIProvider\n→ Responses API"]
    R --> G["GeminiProvider\n→ generate_content"]
    A --> N["LLMResponse\n(normalized)"]
    O --> N
    G --> N
```

The differences the adapters absorb run deep:

| | Anthropic | OpenAI | Gemini |
|---|---|---|---|
| API | Messages | Responses | `generate_content` |
| Prompt caching | `cache_control` breakpoints on system/tool blocks | `prompt_cache_key` + retention | server-side `CachedContent` with TTL |
| Streaming | yes | yes | not yet |
| Structured output | `output_config` json_schema | `text.format` json_schema | `response_mime_type` + `response_schema` |
| Quirks | beta flags via `provider_options` | temperature omitted for `gpt-5*` | schema types coerced to `"OBJECT"` uppercase |

Prompt caching is the sharpest example. The developer-facing surface is one boolean — `prompt_caching_enabled` in `AgentConfig`, on by default — which flows into the request as `use_prompt_caching`. What happens next differs completely per vendor: the Anthropic adapter annotates system and tool blocks with cache breakpoints; the OpenAI adapter attaches a cache key with configurable retention; the Gemini adapter creates an actual server-side cache resource holding the system instruction and tool definitions, references it on subsequent requests, recreates it when system or tools change, and cleans it up on `close()`. If cache creation fails, it silently falls back to uncached requests. All of it stays inside the adapter: an agent spec that switches `AnthropicProvider` for `GeminiProvider` changes one line.

## Streaming without a second contract

`send()` stays the single generation entry point with the same return type whether or not the response streams.

When `request.streaming` is set and the adapter supports it, the provider streams *internally*. As text arrives it emits coalesced `llm.response.delta` events — the frames you saw riding the SSE stream in [the request lifecycle post](request-lifecycle.md) — and then returns the fully accumulated `LLMResponse`, identical in shape to the non-streaming case. The coalescing is deliberate: chunks are flushed by size or interval so event volume stays at tens per turn. `llm.request.complete` remains the source of truth for duration and token counts; deltas are a progress channel.

Adapters without streaming support — Gemini, currently — ignore the hint and return the same response shape; the answer simply arrives all at once.

## Structured output, per provider

The structured-output flow from the runtime ([finalize_structured_output](durable-agent-loop.md), the second LLM call after a run completes) drives providers through `provider_options["structured_output"]` — a JSON schema dict. Each adapter detects the key and translates it to its native schema-enforcement feature, listed in the table above. The instruction *asking* the model to produce the payload is added by the runtime; the adapters enforce shape. The result is that `structured_output=MyPydanticModel` on a request behaves the same against all three vendors.

`provider_options` itself is the designated escape hatch — beta flags for Anthropic, reasoning controls for OpenAI, cache TTLs for Gemini. It's treated as adapter-specific by design: anything that needs to work everywhere gets promoted to a real `LLMRequest` field, and anything vendor-specific stays in the dict, marked as such by where it lives.

## What the contract buys the rest of the system

Two consequences ripple through everything covered so far in this series.

First, the durable loop serializes context between checkpoints — which is possible because messages and content blocks are normalized types with stable shapes. `coerce_content_blocks` even normalizes legacy forms (`tool_use` → `tool_call`) so stored context from older runs keeps deserializing.

Second, observability gets provider-uniform events for free. Every adapter inherits `llm.request.start` / `llm.request.complete` / `llm.request.error` emission from `BaseLLMProvider`, with model, duration, and token fields in the same places — so the trace analysis at the end of this series sees uniform fields across vendors.

There's one more thing riding on every one of these requests: the `tools` list, serialized in full each time. For a host with many instruction-heavy capabilities that payload gets expensive — Mash's answer is skills, instructions that load on demand.

*Next: [Skills: Instructions on Demand](skills-on-demand.md).*

"""Contained LLM package for provider-neutral contracts and adapters."""

from __future__ import annotations

from .anthropic import DEFAULT_ANTHROPIC_MODEL, AnthropicProvider
from .base import BaseLLMProvider, LLMProvider
from .gemini import DEFAULT_GEMINI_MODEL, GeminiProvider
from .openai import DEFAULT_OPENAI_MODEL, OpenAIProvider
from .oss import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GEMMA_MODEL,
    DEFAULT_LLAMA_MODEL,
    DEFAULT_OSS_BASE_URL,
    DEFAULT_QWEN_MODEL,
    DeepSeekProvider,
    GemmaProvider,
    LlamaProvider,
    OSSCompatibleProvider,
    QwenProvider,
)
from .types import (
    LLMCapabilities,
    LLMContentBlock,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
    LLMToolDefinition,
    coerce_content_blocks,
)

__all__ = [
    "BaseLLMProvider",
    "LLMCapabilities",
    "LLMContentBlock",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMTokenUsage",
    "LLMToolDefinition",
    "coerce_content_blocks",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "OSSCompatibleProvider",
    "GemmaProvider",
    "QwenProvider",
    "DeepSeekProvider",
    "LlamaProvider",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_OSS_BASE_URL",
    "DEFAULT_GEMMA_MODEL",
    "DEFAULT_QWEN_MODEL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_LLAMA_MODEL",
]

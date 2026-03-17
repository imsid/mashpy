"""Contained LLM package for provider-neutral contracts and adapters."""

from __future__ import annotations

from .anthropic import DEFAULT_ANTHROPIC_MODEL, AnthropicProvider
from .base import BaseLLMProvider, LLMProvider
from .openai import DEFAULT_OPENAI_MODEL, OpenAIProvider
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
    "OpenAIProvider",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_OPENAI_MODEL",
]

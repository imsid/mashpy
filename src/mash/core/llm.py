"""LLM provider abstraction for agent execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from .context import ToolCall


class LLMProvider(ABC):
    """Interface for LLM providers."""

    @abstractmethod
    def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
    ) -> Any:
        """Send a message to the LLM provider."""

    @abstractmethod
    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        """Parse provider response into assistant text and tool calls."""


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider implementation."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        """Initialize the Anthropic provider.

        Args:
            api_key: Anthropic API key. If not provided, will use ANTHROPIC_API_KEY env var.
        """
        try:
            self._client = Anthropic(api_key=api_key)
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic client is not installed. Install `anthropic` to use this provider."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Anthropic client. Check your API key."
            ) from exc

    def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
    ) -> Any:
        """Create a message using the Anthropic API.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4").
            system: System prompt.
            messages: List of messages in Anthropic format.
            tools: List of tool definitions in Anthropic format.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            Anthropic API response.
        """
        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }

        if system:
            params["system"] = system

        if tools:
            params["tools"] = tools

        return self._client.messages.create(**params)

    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        """Parse Anthropic response into text, tool calls, and blocks.

        Args:
            response: Anthropic API response.

        Returns:
            Tuple of (assistant_text, tool_calls, content_blocks).
        """
        content = getattr(response, "content", None)
        if content is None:
            return "", [], []

        if isinstance(content, str):
            return content, [], [{"type": "text", "text": content}]

        tool_calls: List[ToolCall] = []
        text_parts: List[str] = []
        blocks: List[Dict[str, Any]] = []

        for block in content:
            block_type = _block_value(block, "type")

            if block_type == "text":
                text = _block_value(block, "text") or ""
                text_parts.append(text)
                blocks.append({"type": "text", "text": text})

            elif block_type == "tool_use":
                tool_id = _block_value(block, "id")
                name = _block_value(block, "name") or ""
                arguments = _block_value(block, "input") or {}

                tool_calls.append(
                    ToolCall(
                        id=str(tool_id),
                        name=str(name),
                        arguments=arguments or {},
                    )
                )
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": arguments,
                    }
                )
            else:
                blocks.append(_coerce_block_dict(block, block_type))

        return "".join(text_parts).strip(), tool_calls, blocks


def _coerce_block_dict(block: Any, block_type: Optional[str]) -> Dict[str, Any]:
    """Convert a block to dictionary format."""
    if isinstance(block, dict):
        return block

    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except TypeError:
            pass

    if hasattr(block, "dict"):
        try:
            return block.dict()
        except TypeError:
            pass

    data: Dict[str, Any] = {}
    raw = getattr(block, "__dict__", None)
    if isinstance(raw, dict):
        data.update(raw)

    if block_type:
        data.setdefault("type", block_type)

    if not data:
        data = {"type": block_type or "unknown", "text": str(block)}

    return data


def _block_value(block: Any, key: str) -> Any:
    """Extract a value from a block."""
    if isinstance(block, dict):
        return block.get(key)

    value = getattr(block, key, None)
    if value is not None:
        return value

    if hasattr(block, "model_dump"):
        try:
            return block.model_dump().get(key)
        except TypeError:
            pass

    if hasattr(block, "dict"):
        try:
            return block.dict().get(key)
        except TypeError:
            pass

    return None

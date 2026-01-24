"""LLM provider abstraction for mashd runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from ..logging import DebugEvent, EventLogger
from .models import ToolCall
from .telemetry import TokenUsage


class LLMProvider(ABC):
    """Interface for LLM providers used by the agent runtime."""

    @abstractmethod
    def create_message(
        self,
        *,
        session_id: str,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        betas: Optional[List[str]] = None,
    ) -> Any:
        """Send a message to the LLM provider."""

    @abstractmethod
    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List["ToolCall"], List[Dict[str, Any]]]:
        """Parse provider response into assistant text and tool calls."""

    @abstractmethod
    def extract_usage(self, response: Any) -> TokenUsage:
        """Extract token usage information from the provider response."""


class AnthropicProvider(LLMProvider):
    """Thin wrapper over the Anthropic client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        event_logger: EventLogger,
        app_id: str,
    ) -> None:
        self._debug_logger = EventLogger(event_logger.destination)
        self._app_id = app_id
        try:
            self._client = Anthropic(api_key=api_key)
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic client is not installed. Install `anthropic` to enable agent mode."
            ) from exc
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Exception raised while initializing Anthropic client."
            ) from exc

    def create_message(
        self,
        *,
        session_id: str,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        betas: Optional[List[str]] = None,
    ) -> Any:
        event = DebugEvent(
            event_type="llm.request",
            app_id=self._app_id,
            session_id=session_id,
            payload={
                "system_prompt": system,
                "tools": tools,
            },
        )
        # self._debug_logger.emit(event)
        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        if betas:
            return self._client.beta.messages.create(**params, betas=betas)
        return self._client.messages.create(**params)

    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List["ToolCall"], List[Dict[str, Any]]]:

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
                        tool_id=str(tool_id), name=str(name), arguments=arguments or {}
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

    def extract_usage(self, response: Any) -> TokenUsage:
        usage = getattr(response, "usage", None)
        if usage is None:
            return TokenUsage()
        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
        return TokenUsage(
            input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0)
        )


def _coerce_block_dict(block: Any, block_type: Optional[str]) -> Dict[str, Any]:
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

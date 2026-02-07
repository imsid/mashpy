"""LLM provider abstraction for agent execution."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from ..logging import EventLogger, LLMEvent
from .config import SystemPrompt
from .context import ToolCall

# Model-specific minimum token thresholds for prompt caching
# Based on Anthropic documentation: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
CACHE_MIN_TOKENS = {
    "claude-haiku": 4096,  # Haiku 4.5 requires 4096 tokens minimum
    "claude-sonnet": 1024,  # Sonnet 4.5 requires 1024 tokens minimum
    "claude-opus": 1024,  # Opus 4 requires 1024 tokens minimum
    "default": 1024,  # Default for unknown models
}


class LLMProvider(ABC):
    """Interface for LLM providers."""

    @abstractmethod
    def create_message(
        self,
        *,
        model: str,
        system: SystemPrompt,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
        betas: Optional[List[str]] = None,
        use_prompt_caching: bool = True,
    ) -> Any:
        """Send a message to the LLM provider."""

    @abstractmethod
    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        """Parse provider response into assistant text, tool calls, and skill calls."""

    @abstractmethod
    def set_event_logger(
        self, logger: EventLogger, session_id: str, app_id: str
    ) -> None:
        """Set the event logger for LLM operations."""

    @abstractmethod
    def set_trace_id(self, trace_id: Optional[str]) -> None:
        """Set the trace ID for the current agent execution.

        Args:
            trace_id: Trace ID to associate with LLM events.
        """


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider implementation."""

    def __init__(
        self,
        app_id: str,
        api_key: Optional[str] = None,
        event_logger: Optional[EventLogger] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Initialize the Anthropic provider.

        Args:
            api_key: Anthropic API key. Uses ANTHROPIC_API_KEY env var if not provided.
            event_logger: Optional event logger for logging LLM operations.
            session_id: Optional session ID for event logging.
            app_id: Optional app ID for event logging.
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

        self._event_logger = event_logger
        self._session_id = session_id
        self._app_id = app_id
        self._trace_id: Optional[str] = None

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        """Set the trace ID for the current agent execution.

        Args:
            trace_id: Trace ID to associate with LLM events.
        """
        self._trace_id = trace_id

    def set_event_logger(
        self, logger: EventLogger, session_id: str, app_id: str
    ) -> None:
        """Set the event logger for LLM operations.

        Args:
            logger: Event logger instance.
            session_id: Session ID for this run.
            app_id: Application ID.
        """
        self._event_logger = logger
        self._session_id = session_id
        self._app_id = app_id

    def _get_cache_threshold(self, model: str) -> int:
        """Get minimum tokens required for prompt caching based on model.

        Different Claude models have different minimum token requirements:
        - Haiku 4.5: 4096 tokens
        - Sonnet 4.5 / Opus 4: 1024 tokens

        Args:
            model: Model name (e.g., "claude-haiku-4-5-20251001").

        Returns:
            Minimum token threshold for prompt caching.
        """
        model_lower = model.lower()
        for model_prefix, threshold in CACHE_MIN_TOKENS.items():
            if model_prefix in model_lower:
                return threshold
        return CACHE_MIN_TOKENS["default"]

    def create_message(
        self,
        *,
        model: str,
        system: SystemPrompt,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
        betas: Optional[List[str]] = None,
        use_prompt_caching: bool = True,
    ) -> Any:
        """Create a message using the Anthropic API.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4").
            system: System prompt.
            messages: List of messages in Anthropic format.
            tools: List of tool definitions in Anthropic format.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            betas: Optional list of beta feature flags.
            use_prompt_caching: Whether to enable prompt caching for static content.

        Returns:
            Anthropic API response.
        """
        request_start = time.time()

        # Prepare tools with optional caching
        tools_param: List[Dict[str, Any]] = []
        tool_names: List[str] = []
        if tools:
            tools_param = tools.copy() if use_prompt_caching else tools
            # Add cache_control to last tool to cache all tools up to that point
            # BUT: Only if tools don't have defer_loading (can't use both)
            if use_prompt_caching and tools_param:
                last_tool = tools_param[-1]
                # Check if last tool has defer_loading
                has_defer_loading = last_tool.get("defer_loading", False)
                if not has_defer_loading:
                    # Only cache if NOT using defer_loading
                    tools_param[-1] = {
                        **tools_param[-1],
                        "cache_control": {"type": "ephemeral"},
                    }
            # Extract tool names for logging
            for tool in tools_param:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name")
                if isinstance(name, str) and name:
                    tool_names.append(name)

        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }

        if system:
            params["system"] = system

        if tools_param:
            params["tools"] = tools_param

        # Log request start
        if self._event_logger:
            self._event_logger.emit(
                LLMEvent(
                    event_type="llm.request.start",
                    app_id=self._app_id,
                    session_id=self._session_id,
                    provider="anthropic",
                    model=model,
                    trace_id=self._trace_id,
                    tools=tool_names,
                    payload={
                        "tools": tools_param,
                        "messages": messages,
                    },
                    betas=betas,
                )
            )
        try:
            # Use beta client when beta flags are provided
            if betas:
                response = self._client.beta.messages.create(**params, betas=betas)
            else:
                response = self._client.messages.create(**params)

            # Log request completion
            if self._event_logger:
                # Extract token usage
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", None) if usage else None
                output_tokens = getattr(usage, "output_tokens", None) if usage else None
                cache_creation_input_tokens = (
                    getattr(usage, "cache_creation_input_tokens", None)
                    if usage
                    else None
                )
                cache_read_input_tokens = (
                    getattr(usage, "cache_read_input_tokens", None) if usage else None
                )
                total_tokens = None
                if input_tokens is not None and output_tokens is not None:
                    total_tokens = input_tokens + output_tokens

                # Extract finish reason
                finish_reason = getattr(response, "stop_reason", None)

                self._event_logger.emit(
                    LLMEvent(
                        event_type="llm.request.complete",
                        app_id=self._app_id,
                        session_id=self._session_id,
                        provider="anthropic",
                        model=model,
                        duration_ms=int((time.time() - request_start) * 1000),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                        cache_creation_input_tokens=cache_creation_input_tokens,
                        cache_read_input_tokens=cache_read_input_tokens,
                        finish_reason=finish_reason,
                        trace_id=self._trace_id,
                        tools=tool_names,
                        betas=betas,
                    )
                )

            return response
        except Exception as e:
            # Log request error
            if self._event_logger:
                self._event_logger.emit(
                    LLMEvent(
                        event_type="llm.request.error",
                        app_id=self._app_id,
                        session_id=self._session_id,
                        provider="anthropic",
                        model=model,
                        error=str(e),
                        duration_ms=int((time.time() - request_start) * 1000),
                        trace_id=self._trace_id,
                        tools=tool_names,
                        betas=betas,
                    )
                )
            raise

    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        """Parse Anthropic response into text, tool calls, and blocks.

        Args:
            response: Anthropic API response.

        Returns:
            Tuple of (assistant_text, tool_calls, skill_used, content_blocks).
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
            elif block_type == "server_tool_use":
                tool_id = _block_value(block, "id")
                name = _block_value(block, "name") or ""
                arguments = _block_value(block, "input") or {}
                caller = _block_value(block, "caller")
                caller_type = (
                    _block_value(caller, "type") if caller is not None else None
                )
                caller_data = (
                    _coerce_block_dict(caller, caller_type)
                    if caller is not None
                    else None
                )

                block_data = {
                    "type": "server_tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": arguments,
                }
                if caller_data is not None:
                    block_data["caller"] = caller_data
                blocks.append(block_data)
            elif block_type == "text_editor_code_execution_tool_result":
                tool_use_id = _block_value(block, "tool_use_id")
                result_content = _block_value(block, "content")
                result_type = _block_value(result_content, "type")
                result_data = (
                    _coerce_block_dict(result_content, result_type)
                    if result_content is not None
                    else None
                )

                blocks.append(
                    {
                        "type": "text_editor_code_execution_tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_data,
                    }
                )
            elif block_type == "bash_code_execution_tool_result":
                tool_use_id = _block_value(block, "tool_use_id")
                result_content = _block_value(block, "content")
                result_type = _block_value(result_content, "type")
                result_data = (
                    _coerce_block_dict(result_content, result_type)
                    if result_content is not None
                    else None
                )

                blocks.append(
                    {
                        "type": "bash_code_execution_tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_data,
                    }
                )
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

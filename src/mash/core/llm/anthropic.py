"""Anthropic provider adapter."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from .base import BaseLLMProvider
from .types import (
    LLMCapabilities,
    LLMContentBlock,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
    LLMToolDefinition,
)
from .utils import block_value, coerce_block_dict

CACHE_MIN_TOKENS = {
    "claude-haiku": 4096,
    "claude-sonnet": 1024,
    "claude-opus": 1024,
    "default": 1024,
}
DEFAULT_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude provider implementation."""

    provider_name = "anthropic"

    def __init__(
        self,
        app_id: str,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: Optional[str] = None,
        event_logger: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            app_id=app_id,
            model=model,
            event_logger=event_logger,
            session_id=session_id,
        )
        self._validate_model(self.model)
        if Anthropic is None:
            raise RuntimeError(
                "Anthropic client is not installed. Install `anthropic` to use this provider."
            )
        resolved_api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not resolved_api_key:
            raise RuntimeError(
                "Anthropic API key is required. Set ANTHROPIC_API_KEY or pass api_key."
            )
        try:
            self._client = Anthropic(api_key=resolved_api_key)
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Anthropic client. Check your API key."
            ) from exc

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(beta_flags=True, server_tools=True)

    def send(self, request: LLMRequest) -> LLMResponse:

        request_start = time.time()
        betas = self._request_betas(request)
        params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": self._anthropic_messages(request),
            "temperature": request.temperature,
        }

        system = self._anthropic_system(
            request.system,
            use_prompt_caching=request.use_prompt_caching,
        )
        if system:
            params["system"] = system

        tools = self._anthropic_tools(
            request.tools,
            use_prompt_caching=request.use_prompt_caching,
        )
        if tools:
            params["tools"] = tools

        self._emit_request_start(
            request,
            payload={"messages": params["messages"], "tools": params.get("tools", [])},
        )

        try:
            if betas:
                raw_response = self._client.beta.messages.create(**params, betas=betas)
            else:
                raw_response = self._client.messages.create(**params)
            response = self._parse_anthropic_response(raw_response)
            self._emit_request_complete(
                request,
                started_at=request_start,
                response=response,
            )
            return response
        except Exception as exc:
            self._emit_request_error(request, started_at=request_start, error=exc)
            raise

    def _get_cache_threshold(self, model: str) -> int:
        model_lower = model.lower()
        for model_prefix, threshold in CACHE_MIN_TOKENS.items():
            if model_prefix in model_lower:
                return threshold
        return CACHE_MIN_TOKENS["default"]

    def _validate_model(self, model: str) -> None:
        if "claude" not in model.lower():
            raise ValueError(
                f"AnthropicProvider requires a Claude model, got {model!r}."
            )

    def _anthropic_system(
        self,
        system: Any,
        *,
        use_prompt_caching: bool,
    ) -> Any:
        if not system:
            return system

        if isinstance(system, str):
            if not use_prompt_caching:
                return system
            return [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        blocks = [
            coerce_block_dict(block, block_value(block, "type")) for block in system
        ]
        if use_prompt_caching and blocks and not blocks[-1].get("cache_control"):
            blocks[-1] = {
                **blocks[-1],
                "cache_control": {"type": "ephemeral"},
            }
        return blocks

    def _anthropic_tools(
        self,
        tools: List[LLMToolDefinition],
        *,
        use_prompt_caching: bool,
    ) -> List[Dict[str, Any]]:
        translated = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters_json_schema,
                **tool.metadata,
            }
            for tool in tools
        ]

        if use_prompt_caching and translated:
            last_tool = translated[-1]
            if not last_tool.get("defer_loading", False):
                translated[-1] = {
                    **last_tool,
                    "cache_control": {"type": "ephemeral"},
                }
        return translated

    def _anthropic_messages(self, request: LLMRequest) -> List[Dict[str, Any]]:
        translated: List[Dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                results = []
                for block in message.content:
                    if block.type != "tool_result":
                        continue
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.data.get("tool_call_id"),
                            "content": block.data.get("content", ""),
                            "is_error": block.data.get("is_error", False),
                        }
                    )
                if results:
                    translated.append({"role": "user", "content": results})
                continue

            role = "assistant" if message.role == "assistant" else "user"
            content: List[Dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    content.append({"type": "text", "text": block.data.get("text", "")})
                elif block.type == "tool_call":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.data.get("id"),
                            "name": block.data.get("name"),
                            "input": block.data.get("arguments", {}),
                        }
                    )
            if content:
                translated.append({"role": role, "content": content})
        return translated

    def _parse_anthropic_response(self, response: Any) -> LLMResponse:
        from ..context import ToolCall

        content = getattr(response, "content", None)
        if content is None:
            return LLMResponse(
                text="",
                tool_calls=[],
                content_blocks=[],
                stop_reason=getattr(response, "stop_reason", None),
                usage=self._anthropic_usage(getattr(response, "usage", None)),
                provider_response=response,
            )

        if isinstance(content, str):
            return LLMResponse(
                text=content,
                tool_calls=[],
                content_blocks=[LLMContentBlock.text(content)],
                stop_reason=getattr(response, "stop_reason", None),
                usage=self._anthropic_usage(getattr(response, "usage", None)),
                provider_response=response,
            )

        tool_calls: List[ToolCall] = []
        text_parts: List[str] = []
        blocks: List[LLMContentBlock] = []

        for block in content:
            block_type = block_value(block, "type")
            if block_type == "text":
                text = block_value(block, "text") or ""
                text_parts.append(text)
                blocks.append(LLMContentBlock.text(text))
            elif block_type == "tool_use":
                tool_id = str(block_value(block, "id") or "")
                name = str(block_value(block, "name") or "")
                arguments = block_value(block, "input") or {}
                tool_calls.append(
                    ToolCall(
                        id=tool_id,
                        name=name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
                blocks.append(
                    LLMContentBlock.tool_call(
                        tool_call_id=tool_id,
                        name=name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            elif block_type:
                blocks.append(
                    LLMContentBlock(
                        type=block_type,
                        data=coerce_block_dict(block, block_type),
                    )
                )

        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            content_blocks=blocks,
            stop_reason=getattr(response, "stop_reason", None),
            usage=self._anthropic_usage(getattr(response, "usage", None)),
            provider_response=response,
        )

    def _anthropic_usage(self, usage: Any) -> Optional[LLMTokenUsage]:
        if usage is None:
            return None

        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        return LLMTokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
        )

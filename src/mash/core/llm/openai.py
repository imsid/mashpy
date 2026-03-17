"""OpenAI provider adapter."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .base import BaseLLMProvider
from .types import (
    LLMCapabilities,
    LLMContentBlock,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
    LLMToolDefinition,
)
from .utils import block_value

DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")


class OpenAIProvider(BaseLLMProvider):
    """OpenAI Responses API provider implementation."""

    provider_name = "openai"

    def __init__(
        self,
        app_id: str,
        model: str = DEFAULT_OPENAI_MODEL,
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
        if OpenAI is None:
            raise RuntimeError(
                "OpenAI client is not installed. Install `openai` to use this provider."
            )
        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
        if not resolved_api_key:
            raise RuntimeError(
                "OpenAI API key is required. Set OPENAI_API_KEY or pass api_key."
            )
        try:
            self._client = OpenAI(api_key=resolved_api_key)
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize OpenAI client. Check your API key."
            ) from exc

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(reasoning_controls=True)

    def send(self, request: LLMRequest) -> LLMResponse:

        request_start = time.time()
        params: Dict[str, Any] = {
            "model": self.model,
            "instructions": self._openai_instructions(request.system),
            "input": self._openai_input(request),
            "max_output_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        tools = self._openai_tools(request.tools)
        if tools:
            params["tools"] = tools

        if request.use_prompt_caching:
            params["prompt_cache_key"] = self._openai_prompt_cache_key(request)
            params["prompt_cache_retention"] = request.provider_options.get(
                "prompt_cache_retention",
                "24h",
            )

        for key, value in request.provider_options.items():
            if key in {"betas", "prompt_cache_retention", "prompt_cache_key"}:
                continue
            params[key] = value

        self._emit_request_start(
            request,
            payload={"input": params["input"], "tools": params.get("tools", [])},
        )
        try:
            raw_response = self._client.responses.create(**params)
            response = self._parse_openai_response(raw_response)
            self._emit_request_complete(
                request,
                started_at=request_start,
                response=response,
            )
            return response
        except Exception as exc:
            self._emit_request_error(request, started_at=request_start, error=exc)
            raise

    def _openai_prompt_cache_key(self, request: LLMRequest) -> str:
        override = request.provider_options.get("prompt_cache_key")
        if isinstance(override, str) and override.strip():
            return override
        system = self._openai_instructions(request.system) or ""
        tool_names = ",".join(tool.name for tool in request.tools)
        return f"{self._app_id}:{self.model}:{hash((system, tool_names))}"

    def _validate_model(self, model: str) -> None:
        if model.lower().startswith("claude"):
            raise ValueError(
                f"OpenAIProvider cannot use an Anthropic model, got {model!r}."
            )

    def _openai_instructions(self, system: Any) -> Optional[str]:
        if not system:
            return None
        if isinstance(system, str):
            return system

        parts: List[str] = []
        for block in system:
            text = block_value(block, "text")
            if text:
                parts.append(str(text))
        combined = "\n".join(parts).strip()
        return combined or None

    def _openai_tools(self, tools: List[LLMToolDefinition]) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_json_schema,
                **tool.metadata,
            }
            for tool in tools
        ]

    def _openai_input(self, request: LLMRequest) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for message in request.messages:
            text_type = "output_text" if message.role == "assistant" else "input_text"
            text_content = [
                {"type": text_type, "text": block.data.get("text", "")}
                for block in message.content
                if block.type == "text"
            ]
            if text_content:
                items.append(
                    {
                        "type": "message",
                        "role": message.role,
                        "content": text_content,
                    }
                )

            for block in message.content:
                if block.type == "tool_call":
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": block.data.get("id"),
                            "name": block.data.get("name"),
                            "arguments": json.dumps(block.data.get("arguments", {})),
                        }
                    )
                elif block.type == "tool_result":
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.data.get("tool_call_id"),
                            "output": block.data.get("content", ""),
                        }
                    )
        return items

    def _parse_openai_response(self, response: Any) -> LLMResponse:
        from ..context import ToolCall

        output = getattr(response, "output", None) or []
        tool_calls: List[ToolCall] = []
        text_parts: List[str] = []
        blocks: List[LLMContentBlock] = []

        for item in output:
            item_type = block_value(item, "type")
            if item_type == "message":
                for content_item in block_value(item, "content") or []:
                    if block_value(content_item, "type") != "output_text":
                        continue
                    text = block_value(content_item, "text") or ""
                    text_parts.append(text)
                    blocks.append(LLMContentBlock.text(text))
            elif item_type == "function_call":
                call_id = str(
                    block_value(item, "call_id") or block_value(item, "id") or ""
                )
                name = str(block_value(item, "name") or "")
                arguments = self._parse_openai_arguments(block_value(item, "arguments"))
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
                blocks.append(
                    LLMContentBlock.tool_call(
                        tool_call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )

        status = getattr(response, "status", None)
        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            content_blocks=blocks,
            stop_reason=(
                "tool_call" if tool_calls else self._map_openai_stop_reason(response)
            ),
            usage=self._openai_usage(getattr(response, "usage", None)),
            provider_response=response,
            provider_metadata={"status": status} if status else {},
        )

    def _parse_openai_arguments(self, raw_arguments: Any) -> Dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _map_openai_stop_reason(self, response: Any) -> str:
        status = getattr(response, "status", None)
        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = block_value(details, "reason")
            if reason == "max_output_tokens":
                return "max_tokens"
            return str(reason or "incomplete")
        if status == "failed":
            return "error"
        return "end_turn"

    def _openai_usage(self, usage: Any) -> Optional[LLMTokenUsage]:
        if usage is None:
            return None

        input_details = getattr(usage, "input_tokens_details", None)
        cache_read_tokens = block_value(input_details, "cached_tokens")
        cache_write_tokens = block_value(input_details, "cache_creation_tokens")
        return LLMTokenUsage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

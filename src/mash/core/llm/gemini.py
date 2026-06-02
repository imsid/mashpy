"""Gemini provider adapter."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

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

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


class GeminiProvider(BaseLLMProvider):
    """Google Gemini provider implementation using the google-genai SDK."""

    provider_name = "gemini"

    def __init__(
        self,
        app_id: str,
        model: str = DEFAULT_GEMINI_MODEL,
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

        if genai is None or types is None:
            raise RuntimeError(
                "Google GenAI client is not installed. Install `google-genai` to use this provider."
            )

        resolved_api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        
        try:
            if resolved_api_key:
                self._client = genai.Client(api_key=resolved_api_key)
            else:
                # Let the SDK resolve it from environment
                self._client = genai.Client()
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Gemini client."
            ) from exc

        self._cached_content_name: Optional[str] = None
        self._cached_content_key: Optional[str] = None
        self._pending_cache_write_tokens: Optional[int] = None

    def capabilities(self) -> LLMCapabilities:
        # Gemini does not natively have reasoning controls or server tools modeled in Mash capabilities
        return LLMCapabilities()

    def _validate_model(self, model: str) -> None:
        model_lower = model.lower()
        if "gemini-2.0" in model_lower or "gemini-1.5" in model_lower:
            raise ValueError(
                f"GeminiProvider does not support legacy models like {model!r}. "
                "Please use current models such as gemini-3.5-flash or gemini-2.5-flash."
            )
        if "gemini" not in model_lower and "gemma" not in model_lower:
            raise ValueError(
                f"GeminiProvider requires a Gemini or Gemma model, got {model!r}."
            )

    def _coerce_schema_types_to_uppercase(self, schema: Any) -> Any:
        """Recursively converts lowercase types in JSON schema (e.g. 'object') to uppercase (e.g. 'OBJECT')."""
        if isinstance(schema, dict):
            coerced = {}
            for k, v in schema.items():
                if k == "type" and isinstance(v, str):
                    coerced[k] = v.upper()
                else:
                    coerced[k] = self._coerce_schema_types_to_uppercase(v)
            return coerced
        elif isinstance(schema, list):
            return [self._coerce_schema_types_to_uppercase(item) for item in schema]
        else:
            return schema

    def _gemini_instructions(self, system: Any) -> Optional[str]:
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

    def _build_call_id_to_name(self, request: LLMRequest) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for message in request.messages:
            for block in message.content:
                if block.type == "tool_call":
                    call_id = block.data.get("id", "")
                    name = block.data.get("name", "")
                    if call_id and name:
                        mapping[call_id] = name
        return mapping

    def _gemini_contents(self, request: LLMRequest) -> List[types.Content]:
        call_id_to_name = self._build_call_id_to_name(request)
        contents = []
        for message in request.messages:
            # Map role
            if message.role == "tool":
                role = "user"
            else:
                role = "model" if message.role == "assistant" else "user"

            parts = []
            for block in message.content:
                if block.type == "text":
                    parts.append(types.Part.from_text(text=block.data.get("text", "")))
                elif block.type == "tool_call":
                    parts.append(
                        types.Part.from_function_call(
                            name=block.data.get("name", ""),
                            args=block.data.get("arguments", {}),
                        )
                    )
                elif block.type == "tool_result":
                    call_id = block.data.get("tool_call_id", "")
                    func_name = call_id_to_name.get(call_id, call_id)
                    parts.append(
                        types.Part.from_function_response(
                            name=func_name,
                            response={"result": block.data.get("content", "")},
                        )
                    )

            if parts:
                contents.append(types.Content(role=role, parts=parts))
        return contents

    def _cache_key(self, system: Any, tools: List[LLMToolDefinition]) -> str:
        system_text = self._gemini_instructions(system) or ""
        tool_data = [
            {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
            for t in tools
        ]
        blob = json.dumps({"system": system_text, "tools": tool_data}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _build_tools(self, tools: List[LLMToolDefinition]) -> List:
        if not tools:
            return []
        declarations = []
        for tool in tools:
            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters=self._coerce_schema_types_to_uppercase(tool.parameters_json_schema),
                )
            )
        return [types.Tool(function_declarations=declarations)]

    async def _ensure_cache(self, request: LLMRequest) -> Optional[str]:
        if not request.use_prompt_caching:
            return None

        key = self._cache_key(request.system, request.tools)
        if self._cached_content_name and self._cached_content_key == key:
            return self._cached_content_name

        await self._delete_cache()

        system_instruction = self._gemini_instructions(request.system)
        gemini_tools = self._build_tools(request.tools)
        ttl = request.provider_options.get("cache_ttl", "3600s")

        try:
            cache_config = types.CreateCachedContentConfig(
                system_instruction=system_instruction,
                ttl=ttl,
            )
            if gemini_tools:
                cache_config.tools = gemini_tools

            cached = await self._client.aio.caches.create(
                model=self.model,
                config=cache_config,
            )
            self._cached_content_name = cached.name
            self._cached_content_key = key
            um = getattr(cached, "usage_metadata", None)
            self._pending_cache_write_tokens = getattr(um, "total_token_count", None)
            return cached.name
        except Exception:
            self._cached_content_name = None
            self._cached_content_key = None
            return None

    async def _delete_cache(self) -> None:
        if self._cached_content_name:
            try:
                await self._client.aio.caches.delete(name=self._cached_content_name)
            except Exception:
                pass
            self._cached_content_name = None
            self._cached_content_key = None
            self._pending_cache_write_tokens = None

    async def close(self) -> None:
        await self._delete_cache()

    def _gemini_config(
        self, request: LLMRequest, cached_content_name: Optional[str] = None
    ) -> types.GenerateContentConfig:
        # Build structured output
        response_mime_type = None
        response_schema = None
        structured_output = request.provider_options.get("structured_output")
        if isinstance(structured_output, dict):
            response_mime_type = "application/json"
            response_schema = self._coerce_schema_types_to_uppercase(structured_output)

        if cached_content_name:
            config = types.GenerateContentConfig(
                temperature=request.temperature,
                max_output_tokens=request.max_tokens,
                cached_content=cached_content_name,
            )
        else:
            system_instruction = self._gemini_instructions(request.system)
            gemini_tools = self._build_tools(request.tools)
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=request.temperature,
                max_output_tokens=request.max_tokens,
            )
            if gemini_tools:
                config.tools = gemini_tools
                config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)

        if response_mime_type:
            config.response_mime_type = response_mime_type
            config.response_schema = response_schema

        for key, value in request.provider_options.items():
            if key in {"structured_output", "cache_ttl"}:
                continue
            setattr(config, key, value)

        return config

    def _parse_gemini_response(self, response: Any) -> LLMResponse:
        from ..context import ToolCall

        tool_calls: List[ToolCall] = []
        text_parts: List[str] = []
        blocks: List[LLMContentBlock] = []

        if response.candidates:
            candidate = response.candidates[0]
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.text is not None:
                        text_parts.append(part.text)
                        blocks.append(LLMContentBlock.text(part.text))
                    elif part.function_call is not None:
                        call = part.function_call
                        call_id = f"call_{uuid.uuid4().hex[:8]}"
                        arguments = call.args or {}
                        if not isinstance(arguments, dict):
                            try:
                                arguments = dict(arguments)
                            except Exception:
                                arguments = {}
                        tool_calls.append(ToolCall(id=call_id, name=call.name, arguments=arguments))
                        blocks.append(
                            LLMContentBlock.tool_call(
                                tool_call_id=call_id,
                                name=call.name,
                                arguments=arguments,
                            )
                        )

        # Map finish reason
        finish_reason = None
        if response.candidates:
            candidate = response.candidates[0]
            if candidate.finish_reason:
                if hasattr(candidate.finish_reason, "name"):
                    finish_reason = candidate.finish_reason.name
                else:
                    finish_reason = str(candidate.finish_reason)

        stop_reason = "end_turn"
        if tool_calls:
            stop_reason = "tool_call"
        elif finish_reason:
            reason_lower = finish_reason.lower()
            if reason_lower == "stop":
                stop_reason = "end_turn"
            elif reason_lower == "max_tokens":
                stop_reason = "max_tokens"
            else:
                stop_reason = reason_lower

        # Map usage
        usage = None
        if response.usage_metadata:
            um = response.usage_metadata
            usage = LLMTokenUsage(
                input_tokens=getattr(um, "prompt_token_count", None),
                output_tokens=getattr(um, "candidates_token_count", None),
                total_tokens=getattr(um, "total_token_count", None),
                cache_read_tokens=getattr(um, "cached_content_token_count", None),
            )

        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            content_blocks=blocks,
            stop_reason=stop_reason,
            usage=usage,
            provider_response=response,
        )

    async def send(self, request: LLMRequest) -> LLMResponse:
        request_start = time.time()

        cached_content_name = await self._ensure_cache(request)
        contents = self._gemini_contents(request)
        config = self._gemini_config(request, cached_content_name=cached_content_name)

        await self._emit_request_start(
            request,
            payload={
                "contents": [
                    {"role": c.role, "parts": [{"text": p.text, "function_call": getattr(p, "function_call", None)} for p in c.parts]}
                    for c in contents
                ],
                "tools": request.provider_options.get("tools", []),
            },
        )

        try:
            raw_response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            response = self._parse_gemini_response(raw_response)

            if self._pending_cache_write_tokens is not None and response.usage:
                response.usage.cache_write_tokens = self._pending_cache_write_tokens
                self._pending_cache_write_tokens = None

            await self._emit_request_complete(
                request,
                started_at=request_start,
                response=response,
            )
            return response
        except Exception as exc:
            await self._emit_request_error(request, started_at=request_start, error=exc)
            raise

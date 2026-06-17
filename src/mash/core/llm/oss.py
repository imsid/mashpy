"""Open-source model provider over the OpenAI Chat Completions wire.

``OSSCompatibleProvider`` talks to any endpoint that speaks the Chat Completions
API — self-hosted engines (vLLM, Ollama, llama.cpp's ``llama-server``, SGLang,
TGI) and hosted gateways (Together, Fireworks, Groq, OpenRouter). Mash never
runs inference itself; this adapter is a client pointed at a ``base_url``.

The family presets (:class:`GemmaProvider`, :class:`QwenProvider`,
:class:`DeepSeekProvider`, :class:`LlamaProvider`) are thin subclasses that pin a
default model and a capability profile. Everything that makes an OSS model behave
inside the agent
harness lives here, so the agent loop above is unaffected: swapping the provider
in ``AgentSpec.build_llm()`` is the only change a developer makes.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
import time
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

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

DEFAULT_OSS_BASE_URL = os.getenv("OSS_BASE_URL", "http://localhost:11434/v1")
DEFAULT_GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma3")
DEFAULT_QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3")
DEFAULT_DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v3")
DEFAULT_LLAMA_MODEL = os.getenv("LLAMA_MODEL", "llama3.3")


class OSSCompatibleProvider(BaseLLMProvider):
    """Provider for OSS models served over the Chat Completions API.

    Subclasses set ``DEFAULT_MODEL`` and override :meth:`capabilities` to
    declare what the model supports. The base class declares native tool
    calling and streaming, which the latest popular OSS models support.

    Tool use requires native tool calling: the model must be served by a runtime
    that accepts ``tools=`` and emits ``message.tool_calls``. Models without it
    are out of scope — a request carrying tools against a provider with
    ``native_tool_calling=False`` raises rather than silently dropping them.
    """

    provider_name = "oss"
    DEFAULT_MODEL = ""

    def __init__(
        self,
        app_id: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        event_logger: Optional[Any] = None,
        session_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            app_id=app_id,
            model=model or self.DEFAULT_MODEL,
            event_logger=event_logger,
            session_id=session_id,
        )
        if AsyncOpenAI is None:
            raise RuntimeError(
                "OpenAI client is not installed. Install `openai` to use this provider."
            )
        resolved_base_url = base_url or DEFAULT_OSS_BASE_URL
        # Self-hosted engines usually need no key; the SDK still requires a
        # non-empty value, so fall back to a placeholder.
        resolved_api_key = (
            api_key or os.getenv("OSS_API_KEY", "").strip() or "not-needed"
        )
        try:
            self._client = AsyncOpenAI(
                base_url=resolved_base_url, api_key=resolved_api_key
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize OSS client for base_url {resolved_base_url!r}."
            ) from exc

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(streaming=True, native_tool_calling=True)

    async def send(self, request: LLMRequest) -> LLMResponse:
        request_start = time.time()
        caps = self.capabilities()

        messages = self._to_chat_messages(request, caps)
        params: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        # The OSS adapter supports only models served with native tool calling.
        # If tools are present but the provider declares otherwise, fail fast
        # rather than silently dropping the tools (which would let the model
        # answer in prose and never invoke them).
        if request.tools:
            if not caps.native_tool_calling:
                raise ValueError(
                    f"{type(self).__name__} requires native tool calling, but "
                    "capabilities().native_tool_calling is False. The OSS adapter "
                    "only supports models served with native tool calling; serve "
                    "the model with a tool-calling-capable runtime or remove the "
                    "tools from the request."
                )
            params["tools"] = self._to_chat_tools(request.tools)

        # Structured output. When the model supports it, constrain decoding with
        # the standard Chat Completions ``response_format`` json_schema (honored
        # by vLLM/SGLang/llama.cpp and recent Ollama). Otherwise the schema was
        # injected into the system prompt by ``_to_chat_messages`` and we just
        # parse the resulting text.
        schema = request.provider_options.get("structured_output")
        if (
            isinstance(schema, dict)
            and caps.structured_output
            and "response_format" not in request.provider_options
        ):
            params["response_format"] = self._response_format(request, schema)

        # Prompt caching is intentionally ignored: OSS engines (e.g. vLLM)
        # prefix-cache server-side with no request annotation, so the
        # ``use_prompt_caching`` hint has no Chat Completions equivalent to set.

        # Pass through unknown provider_options (e.g. top_p, extra_body).
        for key, value in request.provider_options.items():
            if key in {
                "betas",
                "structured_output",
                "structured_output_strict",
            }:
                continue
            params[key] = value

        await self._emit_request_start(
            request,
            payload={"messages": messages, "tools": params.get("tools", [])},
        )
        try:
            if request.streaming and caps.streaming:
                raw_response = await self._stream(request, params)
            else:
                raw_response = await self._client.chat.completions.create(**params)
            response = self._parse(raw_response, caps)
            await self._emit_request_complete(
                request, started_at=request_start, response=response
            )
            return response
        except Exception as exc:
            await self._emit_request_error(request, started_at=request_start, error=exc)
            raise

    # -- request translation -------------------------------------------------

    def _system_text(self, system: Any) -> Optional[str]:
        if not system:
            return None
        if isinstance(system, str):
            return system or None
        parts: List[str] = []
        for block in system:
            text = block_value(block, "text")
            if text:
                parts.append(str(text))
        combined = "\n".join(parts).strip()
        return combined or None

    def _to_chat_messages(
        self, request: LLMRequest, caps: LLMCapabilities
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        system_text = self._system_text(request.system)

        # Structured-output fallback for models without guided decoding: describe
        # the schema in the system prompt so the model emits conforming JSON.
        schema = request.provider_options.get("structured_output")
        if isinstance(schema, dict) and not caps.structured_output:
            instruction = self._structured_output_instruction(schema)
            system_text = (
                f"{system_text}\n\n{instruction}" if system_text else instruction
            )

        if system_text:
            messages.append({"role": "system", "content": system_text})

        for message in request.messages:
            text_parts = [
                block.data.get("text", "")
                for block in message.content
                if block.type == "text"
            ]
            tool_calls = [
                {
                    "id": block.data.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.data.get("name"),
                        "arguments": json.dumps(block.data.get("arguments", {})),
                    },
                }
                for block in message.content
                if block.type == "tool_call"
            ]
            tool_results = [
                block for block in message.content if block.type == "tool_result"
            ]

            if tool_calls:
                assistant: Dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": tool_calls,
                }
                content = "".join(text_parts).strip()
                if content:
                    assistant["content"] = content
                messages.append(assistant)
            elif text_parts:
                messages.append(
                    {"role": message.role, "content": "".join(text_parts)}
                )

            for block in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.data.get("tool_call_id"),
                        "content": block.data.get("content", ""),
                    }
                )
        return messages

    def _to_chat_tools(
        self, tools: List[LLMToolDefinition]
    ) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                },
            }
            for tool in tools
        ]

    # -- structured output ---------------------------------------------------

    def _structured_output_name(self, schema: Dict[str, Any]) -> str:
        name = str(schema.get("title") or "StructuredOutput").strip()
        normalized = "".join(
            ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name
        )
        return normalized or "StructuredOutput"

    def _response_format(
        self, request: LLMRequest, schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": self._structured_output_name(schema),
                "schema": schema,
                "strict": bool(
                    request.provider_options.get("structured_output_strict", True)
                ),
            },
        }

    def _structured_output_instruction(self, schema: Dict[str, Any]) -> str:
        return (
            "You must respond with a single JSON object that conforms to this "
            "JSON Schema. Output only the JSON object, with no surrounding prose "
            "or code fences:\n" + json.dumps(schema)
        )

    # -- response parsing ----------------------------------------------------

    def _parse(self, raw: Any, caps: LLMCapabilities) -> LLMResponse:
        # Local import: ``context`` imports ``llm.types``, so a top-level import
        # here is a circular import when ``context`` is loaded first. Matches the
        # other providers (e.g. ``OpenAIProvider._parse_openai_response``).
        from ..context import ToolCall  # pylint: disable=import-outside-toplevel

        choices = getattr(raw, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None) if choice is not None else None

        text = (block_value(message, "content") or "") if message is not None else ""
        raw_tool_calls = (
            (block_value(message, "tool_calls") or []) if message is not None else []
        )

        reasoning: Optional[str] = None
        if caps.reasoning_content and message is not None:
            text, reasoning = self._split_reasoning(message, text)

        tool_calls: List[ToolCall] = []
        blocks: List[LLMContentBlock] = []
        if text:
            blocks.append(LLMContentBlock.text(text))
        for call in raw_tool_calls:
            call_id = str(block_value(call, "id") or "")
            function = block_value(call, "function")
            name = str(block_value(function, "name") or "")
            arguments = self._parse_arguments(block_value(function, "arguments"))
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
            blocks.append(
                LLMContentBlock.tool_call(
                    tool_call_id=call_id, name=name, arguments=arguments
                )
            )

        finish_reason = (
            block_value(choice, "finish_reason") if choice is not None else None
        )
        provider_metadata: Dict[str, Any] = {}
        if finish_reason:
            provider_metadata["finish_reason"] = finish_reason
        if reasoning:
            provider_metadata["reasoning"] = reasoning
        return LLMResponse(
            text=text.strip(),
            tool_calls=tool_calls,
            content_blocks=blocks,
            stop_reason=(
                "tool_call" if tool_calls else self._map_stop_reason(finish_reason)
            ),
            usage=self._usage(getattr(raw, "usage", None)),
            provider_response=raw,
            provider_metadata=provider_metadata,
        )

    def _split_reasoning(self, message: Any, text: str) -> tuple[str, Optional[str]]:
        """Separate model thinking from the visible answer.

        Prefers a dedicated ``reasoning_content`` field (DeepSeek-R1 style, as
        surfaced by vLLM/SGLang reasoning parsers); otherwise strips an inline
        ``<think>...</think>`` block from the content. Keeps the reasoning out of
        the transcript while preserving it in ``provider_metadata``.
        """
        reasoning = block_value(message, "reasoning_content")
        if reasoning:
            return text, str(reasoning)
        match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
        if match:
            stripped = (text[: match.start()] + text[match.end() :]).strip()
            return stripped, match.group(1).strip()
        return text, None

    def _parse_arguments(self, raw_arguments: Any) -> Dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}  # tolerant: weaker models emit malformed arguments
        return parsed if isinstance(parsed, dict) else {}

    def _map_stop_reason(self, finish_reason: Any) -> str:
        if finish_reason == "length":
            return "max_tokens"
        if finish_reason == "tool_calls":
            return "tool_call"
        if finish_reason == "stop":
            return "end_turn"
        return str(finish_reason or "end_turn")

    def _usage(self, usage: Any) -> Optional[LLMTokenUsage]:
        if usage is None:
            return None
        prompt_details = block_value(usage, "prompt_tokens_details")
        cache_read_tokens = (
            block_value(prompt_details, "cached_tokens")
            if prompt_details is not None
            else None
        )
        return LLMTokenUsage(
            input_tokens=block_value(usage, "prompt_tokens"),
            output_tokens=block_value(usage, "completion_tokens"),
            total_tokens=block_value(usage, "total_tokens"),
            cache_read_tokens=cache_read_tokens,
        )

    # -- streaming -----------------------------------------------------------

    async def _stream(self, request: LLMRequest, params: Dict[str, Any]) -> Any:
        """Stream chat-completion chunks and accumulate a full response.

        Pushes content deltas to the coalescing delta stream (emitting
        ``llm.response.delta`` events) and reassembles fragmented tool-call
        deltas by index, then returns a response-shaped object so :meth:`_parse`
        handles the streamed and non-streamed paths identically.
        """
        stream_params = dict(params)
        stream_params["stream"] = True
        stream_params["stream_options"] = {"include_usage": True}

        deltas = self._delta_stream(request)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_fragments: Dict[int, Dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        usage: Any = None

        stream = await self._client.chat.completions.create(**stream_params)
        async for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = block_value(choice, "finish_reason") or finish_reason
            delta = block_value(choice, "delta")
            if delta is None:
                continue

            content = block_value(delta, "content")
            if content:
                content_parts.append(content)
                await deltas.push(content)

            reasoning = block_value(delta, "reasoning_content")
            if reasoning:
                reasoning_parts.append(reasoning)

            for fragment in block_value(delta, "tool_calls") or []:
                self._accumulate_tool_fragment(tool_fragments, fragment)
        await deltas.flush()

        return self._assemble_response(
            content_parts, reasoning_parts, tool_fragments, finish_reason, usage
        )

    def _accumulate_tool_fragment(
        self, tool_fragments: Dict[int, Dict[str, Any]], fragment: Any
    ) -> None:
        index = block_value(fragment, "index") or 0
        slot = tool_fragments.setdefault(index, {"id": None, "name": None, "args": ""})
        frag_id = block_value(fragment, "id")
        if frag_id:
            slot["id"] = frag_id
        function = block_value(fragment, "function")
        if function is not None:
            name = block_value(function, "name")
            if name:
                slot["name"] = name
            args = block_value(function, "arguments")
            if args:
                slot["args"] += args

    def _assemble_response(
        self,
        content_parts: List[str],
        reasoning_parts: List[str],
        tool_fragments: Dict[int, Dict[str, Any]],
        finish_reason: Optional[str],
        usage: Any,
    ) -> SimpleNamespace:
        tool_calls = [
            SimpleNamespace(
                id=slot["id"],
                type="function",
                function=SimpleNamespace(
                    name=slot["name"], arguments=slot["args"]
                ),
            )
            for _, slot in sorted(tool_fragments.items())
        ]
        message = SimpleNamespace(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts) or None,
            tool_calls=tool_calls or None,
        )
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        return SimpleNamespace(choices=[choice], usage=usage)


# Presets enable ``structured_output`` because the engines that serve these
# models (vLLM/SGLang/llama.cpp, recent Ollama) support ``response_format``
# json_schema. ``reasoning_content`` is opt-in per preset: a model only gets it
# when its reasoning output would otherwise leak into the transcript (see
# ``GemmaProvider``), since whether a server emits a reasoning channel depends on
# the model and serving flags.
class QwenProvider(OSSCompatibleProvider):
    """Qwen served over a Chat Completions endpoint."""

    provider_name = "qwen"
    DEFAULT_MODEL = DEFAULT_QWEN_MODEL

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            streaming=True, native_tool_calling=True, structured_output=True
        )


class GemmaProvider(OSSCompatibleProvider):
    """Gemma served over a Chat Completions endpoint.

    ``reasoning_content`` is enabled because Gemma's reasoning mode emits a
    ``<think>...</think>`` block inline in ``content``; without the flag that
    block leaks into the transcript instead of being split into
    ``provider_metadata``. The split is safe when reasoning is off: the field is
    absent and the regex simply doesn't match, leaving the text unchanged.
    """

    provider_name = "gemma"
    DEFAULT_MODEL = DEFAULT_GEMMA_MODEL

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            streaming=True,
            native_tool_calling=True,
            structured_output=True,
            reasoning_content=True,
        )


class DeepSeekProvider(OSSCompatibleProvider):
    """DeepSeek served over a Chat Completions endpoint."""

    provider_name = "deepseek"
    DEFAULT_MODEL = DEFAULT_DEEPSEEK_MODEL

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            streaming=True, native_tool_calling=True, structured_output=True
        )


class LlamaProvider(OSSCompatibleProvider):
    """Llama served over a Chat Completions endpoint."""

    provider_name = "llama"
    DEFAULT_MODEL = DEFAULT_LLAMA_MODEL

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            streaming=True, native_tool_calling=True, structured_output=True
        )

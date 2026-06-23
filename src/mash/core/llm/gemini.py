"""Gemini provider adapter."""

from __future__ import annotations

import json
import os
import time
import uuid
import warnings
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional, cast

try:
    from google import genai
    from google.genai import interactions as _gi_interactions
except ImportError:
    genai = None
    _gi_interactions = None

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
    """Google Gemini provider using the Interactions API for server-side session state."""

    provider_name = "gemini"

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        app_id: str,
        model: str = DEFAULT_GEMINI_MODEL,
        api_key: Optional[str] = None,
        event_logger: Optional[Any] = None,
        session_id: Optional[str] = None,
        stateful: bool = False,
        web_search: bool = False,
    ) -> None:
        """
        Args:
            web_search: When True, adds Gemini's native ``google_search`` grounding tool
                to every request, regardless of whether a ``WebSearchProvider`` / MCP
                web-search tool is registered. Defaults to False to preserve existing
                behaviour.
        """
        super().__init__(
            app_id=app_id,
            model=model,
            event_logger=event_logger,
            session_id=session_id,
        )
        self._validate_model(self.model)

        if genai is None or _gi_interactions is None:
            raise RuntimeError(
                "Google GenAI client is not installed. Install `google-genai` to use this provider."
            )

        resolved_api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )

        try:
            if resolved_api_key:
                self._client = genai.Client(api_key=resolved_api_key)
            else:
                self._client = genai.Client()
        except Exception as exc:
            raise RuntimeError("Failed to initialize Gemini client.") from exc

        # Stateful mode: chain turns via previous_interaction_id instead of resending full history.
        self._stateful = stateful
        self._web_search = web_search
        self._interaction_ids: Dict[str, str] = {}
        self._sent_message_counts: Dict[str, int] = {}

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            structured_output=True,
            streaming=True,
            reasoning_content=True,
            reasoning_controls=True,
        )

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
        """Recursively coerce lowercase JSON Schema type values to uppercase for the Gemini API."""
        if isinstance(schema, dict):
            coerced = {}
            for k, v in schema.items():
                if k == "type" and isinstance(v, str):
                    coerced[k] = v.upper()
                else:
                    coerced[k] = self._coerce_schema_types_to_uppercase(v)
            return coerced
        if isinstance(schema, list):
            return [self._coerce_schema_types_to_uppercase(item) for item in schema]
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

    # Tool names registered by Mash's WebSearchProvider convention.
    _WEB_SEARCH_TOOL_NAMES = frozenset(("web_search", "web_fetch"))

    def _build_interaction_tools(self, tools: List[LLMToolDefinition]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        native_search_added = self._web_search
        if self._web_search:
            result.append({"type": "google_search"})
        if not tools:
            return result
        for tool in tools:
            if tool.name in self._WEB_SEARCH_TOOL_NAMES:
                if not native_search_added:
                    result.append({"type": "google_search"})
                    native_search_added = True
                # Drop the MCP-backed function declaration; google_search runs server-side.
            else:
                result.append({
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": self._coerce_schema_types_to_uppercase(tool.parameters_json_schema),
                })
        return result

    def _messages_to_steps(
        self, messages: List[Any], call_id_to_name: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Encode the full message history as Interactions API steps."""
        steps: List[Dict[str, Any]] = []
        for message in messages:
            if message.role == "user":
                text_parts = [
                    b.data.get("text", "")
                    for b in message.content
                    if b.type == "text"
                ]
                if text_parts:
                    steps.append({
                        "type": "user_input",
                        "content": [{"type": "text", "text": "\n".join(text_parts)}],
                    })
            elif message.role == "assistant":
                text_parts = [
                    b.data.get("text", "")
                    for b in message.content
                    if b.type == "text"
                ]
                if text_parts:
                    steps.append({
                        "type": "model_output",
                        "content": [{"type": "text", "text": "\n".join(text_parts)}],
                    })
                for block in message.content:
                    if block.type == "tool_call":
                        steps.append({
                            "type": "function_call",
                            "id": block.data.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "name": block.data.get("name", ""),
                            "arguments": block.data.get("arguments", {}),
                        })
            elif message.role == "tool":
                for block in message.content:
                    if block.type == "tool_result":
                        call_id = block.data.get("tool_call_id", "")
                        steps.append({
                            "type": "function_result",
                            "call_id": call_id,
                            "result": block.data.get("content", ""),
                            "is_error": block.data.get("is_error", False),
                            "name": call_id_to_name.get(call_id, ""),
                        })
        return steps

    def _delta_messages_to_steps(
        self, new_messages: List[Any], call_id_to_name: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Encode only new messages as steps, skipping assistant turns already captured server-side."""
        steps: List[Dict[str, Any]] = []
        for message in new_messages:
            if message.role == "assistant":
                # Already captured in the previous interaction's steps on the server.
                continue
            if message.role == "tool":
                for block in message.content:
                    if block.type == "tool_result":
                        call_id = block.data.get("tool_call_id", "")
                        steps.append({
                            "type": "function_result",
                            "call_id": call_id,
                            "result": block.data.get("content", ""),
                            "is_error": block.data.get("is_error", False),
                            "name": call_id_to_name.get(call_id, ""),
                        })
            elif message.role == "user":
                text_parts = [
                    b.data.get("text", "")
                    for b in message.content
                    if b.type == "text"
                ]
                if text_parts:
                    steps.append({
                        "type": "user_input",
                        "content": [{"type": "text", "text": "\n".join(text_parts)}],
                    })
        return steps

    def _parse_interaction_response(  # pylint: disable=too-many-locals,too-many-branches
        self, interaction: Any
    ) -> LLMResponse:
        from ..context import ToolCall  # pylint: disable=import-outside-toplevel

        steps = interaction.steps or []

        # Find the last user-side input boundary so we only parse the model's response.
        last_input_idx = -1
        for i, step in enumerate(steps):
            step_type = getattr(step, "type", None)
            if step_type in ("user_input", "function_result"):
                last_input_idx = i

        response_steps = steps[last_input_idx + 1:]

        text_parts: List[str] = []
        tool_calls: List[Any] = []
        blocks: List[LLMContentBlock] = []

        for step in response_steps:
            step_type = getattr(step, "type", None)
            if step_type == "thought":
                thought_parts = []
                for item in getattr(step, "summary", None) or []:
                    if getattr(item, "type", None) == "text":
                        thought_parts.append(getattr(item, "text", ""))
                if thought_parts:
                    blocks.append(
                        LLMContentBlock(
                            type="thinking",
                            data={"thinking": "".join(thought_parts)},
                        )
                    )
            elif step_type == "model_output":
                for content in getattr(step, "content", None) or []:
                    if getattr(content, "type", None) == "text":
                        text = getattr(content, "text", "")
                        text_parts.append(text)
                        blocks.append(LLMContentBlock.text(text))
            elif step_type == "function_call":
                call_id = step.id
                arguments = dict(getattr(step, "arguments", None) or {})
                tool_calls.append(ToolCall(id=call_id, name=step.name, arguments=arguments))
                blocks.append(
                    LLMContentBlock.tool_call(
                        tool_call_id=call_id,
                        name=step.name,
                        arguments=arguments,
                    )
                )

        stop_reason = "tool_call" if tool_calls else "end_turn"
        status = getattr(interaction, "status", None)
        if not tool_calls and status == "incomplete":
            stop_reason = "max_tokens"

        usage = None
        u = getattr(interaction, "usage", None)
        if u is not None:
            thought_tokens = getattr(u, "total_thought_tokens", None)
            usage = LLMTokenUsage(
                input_tokens=getattr(u, "total_input_tokens", None),
                output_tokens=getattr(u, "total_output_tokens", None),
                total_tokens=getattr(u, "total_tokens", None),
                cache_read_tokens=getattr(u, "total_cached_tokens", None),
                metadata={"thought_tokens": thought_tokens} if thought_tokens else {},
            )

        return LLMResponse(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            content_blocks=blocks,
            stop_reason=stop_reason,
            usage=usage,
            provider_response=interaction,
        )

    async def _stream_response(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self, request: LLMRequest, kwargs: Dict[str, Any]
    ) -> Any:
        """Stream an interaction and return a plain object shaped like a completed Interaction.

        Emits ``llm.response.delta`` events as text arrives. Thought summary
        deltas are accumulated and returned as a synthetic ``thought`` step
        alongside any function_call steps so ``_parse_interaction_response``
        can handle the assembled result uniformly.
        """
        text_parts: List[str] = []
        thought_parts: List[str] = []
        # keyed by step index → {id, name, args_fragments}
        function_calls: Dict[int, Dict[str, Any]] = {}
        final_interaction = None

        deltas = self._delta_stream(request)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = await self._client.aio.interactions.create(stream=True, **kwargs)
        stream = cast(AsyncIterator[Any], raw)

        async for event in stream:
            event_type = getattr(event, "event_type", None)

            if event_type == "step.start":
                step = getattr(event, "step", None)
                if step is not None and getattr(step, "type", None) == "function_call":
                    idx = getattr(event, "index", len(function_calls))
                    function_calls[idx] = {
                        "id": step.id,
                        "name": step.name,
                        "args_fragments": [],
                        # arguments may already be populated on step.start
                        "args_initial": dict(getattr(step, "arguments", None) or {}),
                    }

            elif event_type == "step.delta":
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)
                idx = getattr(event, "index", 0)

                if delta_type == "text":
                    chunk = getattr(delta, "text", "")
                    text_parts.append(chunk)
                    await deltas.push(chunk)

                elif delta_type == "thought_summary":
                    content = getattr(delta, "content", None)
                    if getattr(content, "type", None) == "text":
                        thought_parts.append(getattr(content, "text", ""))

                elif delta_type == "arguments_delta":
                    if idx in function_calls:
                        fragment = getattr(delta, "arguments", "") or ""
                        function_calls[idx]["args_fragments"].append(fragment)

            elif event_type == "interaction.completed":
                final_interaction = getattr(event, "interaction", None)

        await deltas.flush()

        # Build a plain object that _parse_interaction_response can consume.
        steps = []
        if thought_parts:
            steps.append(SimpleNamespace(
                type="thought",
                summary=[SimpleNamespace(type="text", text="".join(thought_parts))],
            ))
        if text_parts:
            steps.append(SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="text", text="".join(text_parts))],
            ))
        for fc in sorted(function_calls.values(), key=lambda x: x.get("id", "")):
            args_text = "".join(fc["args_fragments"])
            args: Dict[str, Any] = fc["args_initial"]
            if args_text:
                try:
                    args = json.loads(args_text)
                except Exception:  # pylint: disable=broad-except
                    pass
            steps.append(SimpleNamespace(
                type="function_call", id=fc["id"], name=fc["name"], arguments=args
            ))

        usage = getattr(final_interaction, "usage", None) if final_interaction else None

        return SimpleNamespace(
            id=getattr(final_interaction, "id", None),
            status=getattr(final_interaction, "status", "completed"),
            steps=steps,
            usage=usage,
        )

    async def close(self) -> None:
        pass

    async def send(self, request: LLMRequest) -> LLMResponse:  # pylint: disable=too-many-locals
        request_start = time.time()

        session_key = self._session_id or ""
        prev_id = self._interaction_ids.get(session_key)
        prev_count = self._sent_message_counts.get(session_key, 0)
        use_chain = self._stateful and prev_id is not None

        call_id_to_name = self._build_call_id_to_name(request)

        if use_chain:
            input_steps = self._delta_messages_to_steps(
                request.messages[prev_count:], call_id_to_name
            )
        else:
            input_steps = self._messages_to_steps(request.messages, call_id_to_name)

        interaction_tools = self._build_interaction_tools(request.tools)
        system_instruction = self._gemini_instructions(request.system)

        await self._emit_request_start(
            request,
            payload={
                "input_steps": len(input_steps),
                "chained": use_chain,
                "previous_interaction_id": prev_id if use_chain else None,
            },
        )

        try:
            generation_config: Dict[str, Any] = {
                "temperature": request.temperature,
                "max_output_tokens": request.max_tokens,
            }
            thinking_level = request.provider_options.get("thinking_level")
            if thinking_level:
                generation_config["thinking_level"] = thinking_level
            thinking_summaries = request.provider_options.get("thinking_summaries")
            if thinking_summaries:
                generation_config["thinking_summaries"] = thinking_summaries

            kwargs: Dict[str, Any] = {
                "model": self.model,
                "input": input_steps,
                "generation_config": generation_config,
            }
            if system_instruction:
                kwargs["system_instruction"] = system_instruction
            if interaction_tools:
                kwargs["tools"] = interaction_tools
            if use_chain:
                kwargs["previous_interaction_id"] = prev_id

            structured_output = request.provider_options.get("structured_output")
            if isinstance(structured_output, dict):
                kwargs["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": structured_output,
                }

            if request.streaming:
                raw = await self._stream_response(request, kwargs)
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    raw = await self._client.aio.interactions.create(**kwargs)

            if self._stateful and raw.id:
                self._interaction_ids[session_key] = raw.id
                self._sent_message_counts[session_key] = len(request.messages)

            response = self._parse_interaction_response(raw)

            await self._emit_request_complete(
                request,
                started_at=request_start,
                response=response,
            )
            return response

        except Exception as exc:
            await self._emit_request_error(request, started_at=request_start, error=exc)
            raise

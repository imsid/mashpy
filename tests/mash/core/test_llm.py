"""Tests for provider-neutral LLM contracts and adapters."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from mash.core.context import ToolCall
from mash.core.llm import AnthropicProvider, OpenAIProvider, GeminiProvider
from mash.core.llm.base import _DeltaStream
from mash.core.llm.types import (
    LLMContentBlock,
    LLMMessage,
    LLMRequest,
    LLMToolDefinition,
)


async def _aiter(items):
    for item in items:
        yield item


class _FakeAnthropicStream:
    """Async-context streaming stub exposing text_stream + get_final_message."""

    def __init__(self, chunks, final):
        self.text_stream = _aiter(chunks)
        self.get_final_message = AsyncMock(return_value=final)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeOpenAIStream:
    """Async-context streaming stub that is itself async-iterable."""

    def __init__(self, events, final):
        self._events = events
        self.get_final_response = AsyncMock(return_value=final)

    def __aiter__(self):
        return _aiter(self._events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class LLMProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_send_omits_temperature_for_gpt5_models(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-5"
        provider._app_id = "test"
        provider._client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock())
        )
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._parse_openai_response = Mock(
            return_value=SimpleNamespace(text="", tool_calls=[], provider_metadata={})
        )
        request = LLMRequest(
            model="gpt-5",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            temperature=0.2,
        )

        await provider.send(request)

        call_kwargs = provider._client.responses.create.call_args.kwargs
        self.assertNotIn("temperature", call_kwargs)

    async def test_openai_send_keeps_temperature_for_non_gpt5_models(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-4.1"
        provider._app_id = "test"
        provider._client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock())
        )
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._parse_openai_response = Mock(
            return_value=SimpleNamespace(text="", tool_calls=[], provider_metadata={})
        )
        request = LLMRequest(
            model="gpt-4.1",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            temperature=0.2,
        )

        await provider.send(request)

        call_kwargs = provider._client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["temperature"], 0.2)

    async def test_openai_send_maps_structured_output_to_text_format(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-4.1"
        provider._app_id = "test"
        provider._client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock())
        )
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._parse_openai_response = Mock(
            return_value=SimpleNamespace(text="{}", tool_calls=[], provider_metadata={})
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        request = LLMRequest(
            model="gpt-4.1",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            provider_options={
                "structured_output": schema,
            },
        )

        await provider.send(request)

        call_kwargs = provider._client.responses.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["text"]["format"],
            {
                "type": "json_schema",
                "name": "StructuredOutput",
                "schema": schema,
                "strict": True,
            },
        )

    async def test_openai_send_streams_when_requested(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-4.1"
        provider._app_id = "test"

        final_response = SimpleNamespace(text="streamed", tool_calls=[], usage=None)
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="hello "),
            SimpleNamespace(type="response.completed"),  # non-text event ignored
            SimpleNamespace(type="response.output_text.delta", delta="world"),
        ]
        stream_obj = _FakeOpenAIStream(events, final_response)
        responses = SimpleNamespace(
            create=AsyncMock(),
            stream=Mock(return_value=stream_obj),
        )
        provider._client = SimpleNamespace(responses=responses)
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._emit_response_delta = AsyncMock()
        provider._parse_openai_response = Mock(
            return_value=SimpleNamespace(text="streamed", tool_calls=[], provider_metadata={})
        )
        request = LLMRequest(
            model="gpt-4.1",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            streaming=True,
        )

        await provider.send(request)

        # Streaming path is used, not the blocking create() call.
        provider._client.responses.stream.assert_called_once()
        provider._client.responses.create.assert_not_called()
        stream_obj.get_final_response.assert_awaited_once()
        # Only text deltas were forwarded (non-text events filtered), coalesced
        # into a single trailing flush.
        provider._emit_response_delta.assert_awaited_once()
        self.assertEqual(
            provider._emit_response_delta.await_args.kwargs["text"], "hello world"
        )
        # The accumulated final response is parsed into the same response shape.
        provider._parse_openai_response.assert_called_once_with(final_response)
        provider._emit_request_complete.assert_awaited_once()

    def test_openai_parser_normalizes_text_and_tool_calls(self) -> None:
        provider = object.__new__(OpenAIProvider)
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="hello")],
                ),
                SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="lookup",
                    arguments='{"query":"test"}',
                ),
            ],
            status="completed",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                input_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
            incomplete_details=None,
        )

        parsed = provider._parse_openai_response(response)

        self.assertEqual(parsed.text, "hello")
        self.assertEqual(parsed.stop_reason, "tool_call")
        self.assertEqual(
            parsed.tool_calls,
            [ToolCall(id="call-1", name="lookup", arguments={"query": "test"})],
        )
        self.assertEqual(parsed.content_blocks[0].to_dict(), {"type": "text", "text": "hello"})
        self.assertEqual(parsed.content_blocks[1].to_dict()["type"], "tool_call")
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.cache_read_tokens, 2)

    def test_openai_input_translates_tool_results(self) -> None:
        provider = object.__new__(OpenAIProvider)
        request = LLMRequest(
            model="gpt-5",
            system="You are helpful.",
            messages=[
                LLMMessage(
                    role="tool",
                    content=[
                        LLMContentBlock.tool_result(
                            tool_call_id="call-1",
                            content="result",
                        )
                    ],
                )
            ],
            tools=[],
            max_tokens=100,
        )

        items = provider._openai_input(request)

        self.assertEqual(
            items,
            [{"type": "function_call_output", "call_id": "call-1", "output": "result"}],
        )

    def test_openai_input_uses_output_text_for_assistant_history(self) -> None:
        provider = object.__new__(OpenAIProvider)
        request = LLMRequest(
            model="gpt-5",
            system="You are helpful.",
            messages=[
                LLMMessage(
                    role="assistant",
                    content=[LLMContentBlock.text("previous reply")],
                )
            ],
            tools=[],
            max_tokens=100,
        )

        items = provider._openai_input(request)

        self.assertEqual(
            items,
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "previous reply"}],
                }
            ],
        )

    def test_anthropic_parser_normalizes_tool_use_blocks(self) -> None:
        provider = object.__new__(AnthropicProvider)
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="done"),
                SimpleNamespace(type="tool_use", id="tool-1", name="search", input={"q": "abc"}),
            ],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=4,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=2,
            ),
        )

        parsed = provider._parse_anthropic_response(response)

        self.assertEqual(parsed.text, "done")
        self.assertEqual(
            parsed.tool_calls,
            [ToolCall(id="tool-1", name="search", arguments={"q": "abc"})],
        )
        self.assertEqual(parsed.content_blocks[1].to_dict()["type"], "tool_call")
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.cache_write_tokens, 3)

    def test_anthropic_tool_translation_preserves_metadata(self) -> None:
        provider = object.__new__(AnthropicProvider)
        tools = [
            LLMToolDefinition(
                name="bash",
                description="Run bash",
                parameters_json_schema={"type": "object"},
                metadata={"category": "shell"},
            )
        ]

        translated = provider._anthropic_tools(tools, use_prompt_caching=True)

        self.assertEqual(translated[0]["name"], "bash")
        self.assertEqual(translated[0]["category"], "shell")
        self.assertIn("cache_control", translated[0])

    async def test_anthropic_send_maps_structured_output_to_output_config(self) -> None:
        provider = object.__new__(AnthropicProvider)
        provider._model = "claude-sonnet-4-5"
        provider._app_id = "test"
        provider._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock()))
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._parse_anthropic_response = Mock(
            return_value=SimpleNamespace(text="{}", tool_calls=[], provider_metadata={})
        )
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        request = LLMRequest(
            model="claude-sonnet-4-5",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            provider_options={
                "structured_output": schema,
            },
        )

        await provider.send(request)

        call_kwargs = provider._client.messages.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["output_config"],
            {
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        )

    async def test_anthropic_send_streams_when_requested(self) -> None:
        provider = object.__new__(AnthropicProvider)
        provider._model = "claude-sonnet-4-5"
        provider._app_id = "test"

        final_message = SimpleNamespace(text="streamed", tool_calls=[], usage=None)
        stream_obj = _FakeAnthropicStream(["hello ", "world"], final_message)
        messages = SimpleNamespace(
            create=AsyncMock(),
            stream=Mock(return_value=stream_obj),
        )
        provider._client = SimpleNamespace(messages=messages)
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._emit_response_delta = AsyncMock()
        provider._parse_anthropic_response = Mock(
            return_value=SimpleNamespace(text="streamed", tool_calls=[], provider_metadata={})
        )
        request = LLMRequest(
            model="claude-sonnet-4-5",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            streaming=True,
        )

        await provider.send(request)

        # Streaming path is used, not the blocking create() call.
        provider._client.messages.stream.assert_called_once()
        provider._client.messages.create.assert_not_called()
        stream_obj.get_final_message.assert_awaited_once()
        # Text deltas were forwarded and coalesced into a single trailing flush.
        provider._emit_response_delta.assert_awaited_once()
        self.assertEqual(
            provider._emit_response_delta.await_args.kwargs["text"], "hello world"
        )
        # The accumulated final message is parsed into the same response shape.
        provider._parse_anthropic_response.assert_called_once_with(final_message)
        provider._emit_request_complete.assert_awaited_once()

    async def test_anthropic_streamed_send_preserves_stop_reason(self) -> None:
        # The streamed path accumulates a final message and parses it with the
        # real parser, so non-end_turn stop reasons (max_tokens, pause_turn) the
        # agent loop relies on must survive streaming exactly as they do on the
        # blocking create() path.
        for stop_reason in ("max_tokens", "pause_turn"):
            with self.subTest(stop_reason=stop_reason):
                provider = object.__new__(AnthropicProvider)
                provider._model = "claude-sonnet-4-5"
                provider._app_id = "test"

                final_message = SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="partial answer")],
                    stop_reason=stop_reason,
                    usage=None,
                )
                stream_obj = _FakeAnthropicStream(["partial ", "answer"], final_message)
                provider._client = SimpleNamespace(
                    messages=SimpleNamespace(
                        create=AsyncMock(),
                        stream=Mock(return_value=stream_obj),
                    )
                )
                provider._emit_request_start = AsyncMock()
                provider._emit_request_complete = AsyncMock()
                provider._emit_request_error = AsyncMock()
                provider._emit_response_delta = AsyncMock()
                request = LLMRequest(
                    model="claude-sonnet-4-5",
                    system="You are helpful.",
                    messages=[],
                    tools=[],
                    max_tokens=100,
                    streaming=True,
                )

                response = await provider.send(request)

                provider._client.messages.stream.assert_called_once()
                provider._client.messages.create.assert_not_called()
                self.assertEqual(response.stop_reason, stop_reason)
                self.assertEqual(response.text, "partial answer")

    async def test_delta_stream_coalesces_by_size_and_flushes_remainder(self) -> None:
        provider = SimpleNamespace(_emit_response_delta=AsyncMock())
        request = LLMRequest(
            model="claude-sonnet-4-5",
            system="s",
            messages=[],
            tools=[],
            max_tokens=10,
        )
        stream = _DeltaStream(provider, request, max_chars=80, max_interval=99.0)

        await stream.push("a" * 50)  # under threshold, buffered
        await stream.push("b" * 50)  # crosses 80 chars -> flush index 0
        await stream.push("c" * 10)  # under threshold, buffered
        await stream.flush()         # trailing remainder -> flush index 1

        calls = provider._emit_response_delta.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["index"], 0)
        self.assertEqual(calls[0].kwargs["text"], "a" * 50 + "b" * 50)
        self.assertEqual(calls[1].kwargs["index"], 1)
        self.assertEqual(calls[1].kwargs["text"], "c" * 10)


class GeminiProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mock_types = SimpleNamespace(
            Content=lambda role, parts: SimpleNamespace(role=role, parts=parts),
            Part=SimpleNamespace(
                from_text=lambda text: SimpleNamespace(text=text, function_call=None),
                from_function_call=lambda name, args: SimpleNamespace(text=None, function_call=SimpleNamespace(name=name, args=args)),
                from_function_response=lambda name, response: SimpleNamespace(text=None, function_response=SimpleNamespace(name=name, response=response)),
            ),
            GenerateContentConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            CreateCachedContentConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            Tool=lambda function_declarations: SimpleNamespace(function_declarations=function_declarations),
            FunctionDeclaration=lambda name, description, parameters: SimpleNamespace(name=name, description=description, parameters=parameters),
            AutomaticFunctionCallingConfig=lambda disable: SimpleNamespace(disable=disable),
        )
        import mash.core.llm.gemini
        self.original_types = mash.core.llm.gemini.types
        mash.core.llm.gemini.types = self.mock_types

    def tearDown(self) -> None:
        import mash.core.llm.gemini
        mash.core.llm.gemini.types = self.original_types

    def test_gemini_model_validation(self) -> None:
        provider = object.__new__(GeminiProvider)
        with self.assertRaises(ValueError):
            provider._validate_model("gemini-2.0-flash")
        with self.assertRaises(ValueError):
            provider._validate_model("gemini-1.5-pro")
        with self.assertRaises(ValueError):
            provider._validate_model("gpt-4")
        provider._validate_model("gemini-3.5-flash")
        provider._validate_model("gemma-4-31b-it")

    def test_gemini_schema_coercion(self) -> None:
        provider = object.__new__(GeminiProvider)
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        }
        coerced = provider._coerce_schema_types_to_uppercase(schema)
        self.assertEqual(
            coerced,
            {
                "type": "OBJECT",
                "properties": {
                    "items": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    }
                }
            }
        )

    def test_gemini_message_translation(self) -> None:
        provider = object.__new__(GeminiProvider)
        request = LLMRequest(
            model="gemini-3.5-flash",
            system="System prompt",
            messages=[
                LLMMessage(role="user", content=[LLMContentBlock.text("Hello")]),
                LLMMessage(role="assistant", content=[LLMContentBlock.tool_call(tool_call_id="call-1", name="search", arguments={"q": "test"})]),
                LLMMessage(role="tool", content=[LLMContentBlock.tool_result(tool_call_id="call-1", content="result")]),
            ],
            tools=[],
            max_tokens=100,
        )
        contents = provider._gemini_contents(request)
        self.assertEqual(len(contents), 3)
        self.assertEqual(contents[0].role, "user")
        self.assertEqual(contents[0].parts[0].text, "Hello")
        
        self.assertEqual(contents[1].role, "model")
        self.assertEqual(contents[1].parts[0].function_call.name, "search")
        self.assertEqual(contents[1].parts[0].function_call.args, {"q": "test"})
        
        self.assertEqual(contents[2].role, "user")
        self.assertEqual(contents[2].parts[0].function_response.name, "search")
        self.assertEqual(contents[2].parts[0].function_response.response, {"result": "result"})

    def test_gemini_config_generation(self) -> None:
        provider = object.__new__(GeminiProvider)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        request = LLMRequest(
            model="gemini-3.5-flash",
            system="System prompt",
            messages=[],
            tools=[
                LLMToolDefinition(
                    name="bash",
                    description="Run bash",
                    parameters_json_schema={"type": "object"}
                )
            ],
            max_tokens=150,
            temperature=0.7,
            provider_options={"structured_output": schema}
        )
        config = provider._gemini_config(request)
        self.assertEqual(config.system_instruction, "System prompt")
        self.assertEqual(config.temperature, 0.7)
        self.assertEqual(config.max_output_tokens, 150)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_schema["type"], "OBJECT")
        self.assertEqual(config.tools[0].function_declarations[0].parameters["type"], "OBJECT")

    def test_gemini_response_parsing(self) -> None:
        provider = object.__new__(GeminiProvider)
        
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(text="Finished task", function_call=None),
                            SimpleNamespace(text=None, function_call=SimpleNamespace(name="lookup", args={"query": "test"})),
                        ]
                    ),
                    finish_reason=SimpleNamespace(name="STOP"),
                )
            ],
            usage_metadata=SimpleNamespace(
                prompt_token_count=20,
                candidates_token_count=10,
                total_token_count=30,
                cached_content_token_count=5,
            )
        )

        parsed = provider._parse_gemini_response(response)
        self.assertEqual(parsed.text, "Finished task")
        self.assertEqual(parsed.stop_reason, "tool_call")
        self.assertEqual(parsed.tool_calls[0].name, "lookup")
        self.assertEqual(parsed.tool_calls[0].arguments, {"query": "test"})
        self.assertEqual(parsed.usage.input_tokens, 20)
        self.assertEqual(parsed.usage.output_tokens, 10)
        self.assertEqual(parsed.usage.total_tokens, 30)
        self.assertEqual(parsed.usage.cache_read_tokens, 5)


class GeminiCachingTests(unittest.IsolatedAsyncioTestCase):
    def _make_provider(self) -> GeminiProvider:
        provider = object.__new__(GeminiProvider)
        provider._model = "gemini-3.5-flash"
        provider._app_id = "test"
        provider._cached_content_name = None
        provider._cached_content_key = None
        provider._pending_cache_write_tokens = None
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()

        mock_types = SimpleNamespace(
            Content=lambda role, parts: SimpleNamespace(role=role, parts=parts),
            Part=SimpleNamespace(
                from_text=lambda text: SimpleNamespace(text=text, function_call=None),
                from_function_call=lambda name, args: SimpleNamespace(text=None, function_call=SimpleNamespace(name=name, args=args)),
                from_function_response=lambda name, response: SimpleNamespace(text=None, function_response=SimpleNamespace(name=name, response=response)),
            ),
            GenerateContentConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            CreateCachedContentConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            Tool=lambda function_declarations: SimpleNamespace(function_declarations=function_declarations),
            FunctionDeclaration=lambda name, description, parameters: SimpleNamespace(name=name, description=description, parameters=parameters),
            AutomaticFunctionCallingConfig=lambda disable: SimpleNamespace(disable=disable),
        )
        import mash.core.llm.gemini
        self._original_types = mash.core.llm.gemini.types
        mash.core.llm.gemini.types = mock_types

        cache_result = SimpleNamespace(
            name="cachedContents/abc123",
            usage_metadata=SimpleNamespace(total_token_count=5000),
        )
        mock_caches = SimpleNamespace(
            create=AsyncMock(return_value=cache_result),
            delete=AsyncMock(),
        )
        generate_response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text="Hello", function_call=None)]
                    ),
                    finish_reason=SimpleNamespace(name="STOP"),
                )
            ],
            usage_metadata=SimpleNamespace(
                prompt_token_count=20,
                candidates_token_count=5,
                total_token_count=25,
                cached_content_token_count=15,
            ),
        )
        mock_models = SimpleNamespace(
            generate_content=AsyncMock(return_value=generate_response),
        )
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(caches=mock_caches, models=mock_models),
        )
        self._mock_caches = mock_caches
        self._mock_models = mock_models
        return provider

    def tearDown(self) -> None:
        import mash.core.llm.gemini
        if hasattr(self, "_original_types"):
            mash.core.llm.gemini.types = self._original_types

    def _make_request(self, **overrides) -> LLMRequest:
        defaults = dict(
            model="gemini-3.5-flash",
            system="You are helpful.",
            messages=[LLMMessage(role="user", content=[LLMContentBlock.text("Hi")])],
            tools=[
                LLMToolDefinition(
                    name="bash", description="Run bash",
                    parameters_json_schema={"type": "object"},
                )
            ],
            max_tokens=100,
            use_prompt_caching=True,
        )
        defaults.update(overrides)
        return LLMRequest(**defaults)

    async def test_cache_created_on_first_send(self) -> None:
        provider = self._make_provider()
        request = self._make_request()
        response = await provider.send(request)

        self._mock_caches.create.assert_called_once()
        config_call = self._mock_models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config_call.cached_content, "cachedContents/abc123")
        self.assertFalse(hasattr(config_call, "system_instruction"))
        self.assertEqual(response.usage.cache_write_tokens, 5000)

    async def test_cache_reused_when_unchanged(self) -> None:
        provider = self._make_provider()
        request = self._make_request()
        await provider.send(request)
        await provider.send(request)

        self._mock_caches.create.assert_called_once()

    async def test_cache_recreated_when_tools_change(self) -> None:
        provider = self._make_provider()
        request1 = self._make_request()
        await provider.send(request1)

        request2 = self._make_request(
            tools=[
                LLMToolDefinition(
                    name="search", description="Search web",
                    parameters_json_schema={"type": "object"},
                )
            ]
        )
        await provider.send(request2)

        self.assertEqual(self._mock_caches.create.call_count, 2)
        self._mock_caches.delete.assert_called_once_with(name="cachedContents/abc123")

    async def test_fallback_when_cache_creation_fails(self) -> None:
        provider = self._make_provider()
        self._mock_caches.create.side_effect = RuntimeError("below minimum token count")
        request = self._make_request()
        response = await provider.send(request)

        config_call = self._mock_models.generate_content.call_args.kwargs["config"]
        self.assertFalse(hasattr(config_call, "cached_content"))
        self.assertEqual(config_call.system_instruction, "You are helpful.")
        self.assertEqual(response.text, "Hello")

    async def test_close_deletes_cache(self) -> None:
        provider = self._make_provider()
        request = self._make_request()
        await provider.send(request)
        await provider.close()

        self._mock_caches.delete.assert_called_once_with(name="cachedContents/abc123")
        self.assertIsNone(provider._cached_content_name)

    async def test_cache_write_tokens_reported_once(self) -> None:
        provider = self._make_provider()
        request = self._make_request()
        r1 = await provider.send(request)
        r2 = await provider.send(request)

        self.assertEqual(r1.usage.cache_write_tokens, 5000)
        self.assertIsNone(r2.usage.cache_write_tokens)

    async def test_caching_skipped_when_disabled(self) -> None:
        provider = self._make_provider()
        request = self._make_request(use_prompt_caching=False)
        await provider.send(request)

        self._mock_caches.create.assert_not_called()
        config_call = self._mock_models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config_call.system_instruction, "You are helpful.")


if __name__ == "__main__":
    unittest.main()

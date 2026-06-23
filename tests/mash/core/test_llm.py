"""Tests for provider-neutral LLM contracts and adapters."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from mash.core.context import ToolCall
from mash.core.llm import (
    AnthropicProvider,
    GeminiProvider,
    GemmaProvider,
    OpenAIProvider,
    OSSCompatibleProvider,
)
from mash.core.llm.base import _DeltaStream
from mash.core.llm.types import (
    LLMCapabilities,
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
    def _make_provider(self, session_id: str = "s-1", stateful: bool = False) -> GeminiProvider:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        provider._model = "gemini-3.5-flash"
        provider._app_id = "test"
        provider._session_id = session_id
        provider._stateful = stateful
        provider._web_search = False
        provider._event_logger = None
        provider._trace_id = None
        provider._interaction_ids: dict = {}
        provider._sent_message_counts: dict = {}
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        return provider

    def _make_interaction(self, text=None, tool_calls=None, status="completed", usage=None):
        """Build a fake Interaction object matching the step.type discriminator shape."""
        steps = [SimpleNamespace(type="user_input", content=[SimpleNamespace(type="text", text="hi")])]
        if text:
            steps.append(SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="text", text=text)],
            ))
        for tc in (tool_calls or []):
            steps.append(SimpleNamespace(
                type="function_call",
                id=tc["id"],
                name=tc["name"],
                arguments=tc["arguments"],
            ))
        fake_usage = SimpleNamespace(
            total_input_tokens=(usage or {}).get("input", 10),
            total_output_tokens=(usage or {}).get("output", 5),
            total_tokens=(usage or {}).get("total", 15),
            total_cached_tokens=(usage or {}).get("cached", None),
        )
        return SimpleNamespace(
            id="interaction-abc",
            status=status,
            steps=steps,
            usage=fake_usage,
        )

    def _make_client(self, interaction):
        mock_create = AsyncMock(return_value=interaction)
        return SimpleNamespace(
            aio=SimpleNamespace(
                interactions=SimpleNamespace(create=mock_create)
            )
        ), mock_create

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
        )
        defaults.update(overrides)
        return LLMRequest(**defaults)

    # --- static helpers ---

    def test_gemini_model_validation(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
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
        provider._web_search = False
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

    def test_messages_to_steps_full_history(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        messages = [
            LLMMessage(role="user", content=[LLMContentBlock.text("Hello")]),
            LLMMessage(role="assistant", content=[
                LLMContentBlock.text("thinking..."),
                LLMContentBlock.tool_call(tool_call_id="call-1", name="search", arguments={"q": "test"}),
            ]),
            LLMMessage(role="tool", content=[
                LLMContentBlock.tool_result(tool_call_id="call-1", content="result text"),
            ]),
        ]
        call_id_to_name = {"call-1": "search"}
        steps = provider._messages_to_steps(messages, call_id_to_name)

        self.assertEqual(steps[0]["type"], "user_input")
        self.assertEqual(steps[0]["content"][0]["text"], "Hello")

        self.assertEqual(steps[1]["type"], "model_output")
        self.assertEqual(steps[1]["content"][0]["text"], "thinking...")

        self.assertEqual(steps[2]["type"], "function_call")
        self.assertEqual(steps[2]["name"], "search")
        self.assertEqual(steps[2]["arguments"], {"q": "test"})

        self.assertEqual(steps[3]["type"], "function_result")
        self.assertEqual(steps[3]["call_id"], "call-1")
        self.assertEqual(steps[3]["result"], "result text")
        self.assertEqual(steps[3]["name"], "search")

    def test_delta_messages_to_steps_skips_assistant(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        new_messages = [
            LLMMessage(role="assistant", content=[
                LLMContentBlock.tool_call(tool_call_id="call-2", name="bash", arguments={"cmd": "ls"}),
            ]),
            LLMMessage(role="tool", content=[
                LLMContentBlock.tool_result(tool_call_id="call-2", content="file.txt"),
            ]),
        ]
        steps = provider._delta_messages_to_steps(new_messages, {"call-2": "bash"})

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["type"], "function_result")
        self.assertEqual(steps[0]["call_id"], "call-2")
        self.assertEqual(steps[0]["result"], "file.txt")

    def test_delta_messages_includes_new_user_turn(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        new_messages = [
            LLMMessage(role="assistant", content=[LLMContentBlock.text("Done.")]),
            LLMMessage(role="user", content=[LLMContentBlock.text("What next?")]),
        ]
        steps = provider._delta_messages_to_steps(new_messages, {})

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["type"], "user_input")
        self.assertEqual(steps[0]["content"][0]["text"], "What next?")

    def test_build_interaction_tools(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        tools = [
            LLMToolDefinition(
                name="bash",
                description="Run bash commands.",
                parameters_json_schema={"type": "object", "properties": {"cmd": {"type": "string"}}},
            )
        ]
        result = provider._build_interaction_tools(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["name"], "bash")
        self.assertEqual(result[0]["parameters"]["type"], "OBJECT")
        self.assertEqual(result[0]["parameters"]["properties"]["cmd"]["type"], "STRING")

    def test_parse_interaction_response_text(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        interaction = self._make_interaction(text="All done.")
        parsed = provider._parse_interaction_response(interaction)

        self.assertEqual(parsed.text, "All done.")
        self.assertEqual(parsed.stop_reason, "end_turn")
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(parsed.usage.input_tokens, 10)
        self.assertEqual(parsed.usage.output_tokens, 5)

    def test_parse_interaction_response_tool_calls(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        interaction = self._make_interaction(
            tool_calls=[{"id": "call-9", "name": "lookup", "arguments": {"q": "sky"}}]
        )
        parsed = provider._parse_interaction_response(interaction)

        self.assertEqual(parsed.stop_reason, "tool_call")
        self.assertEqual(len(parsed.tool_calls), 1)
        self.assertEqual(parsed.tool_calls[0].name, "lookup")
        self.assertEqual(parsed.tool_calls[0].arguments, {"q": "sky"})

    def test_parse_interaction_cached_tokens(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        interaction = self._make_interaction(
            text="ok",
            usage={"input": 100, "output": 20, "total": 120, "cached": 80},
        )
        parsed = provider._parse_interaction_response(interaction)
        self.assertEqual(parsed.usage.cache_read_tokens, 80)

    # --- send() / session chaining ---

    async def test_first_send_uses_full_history(self) -> None:
        provider = self._make_provider()
        interaction = self._make_interaction(text="Hello")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request()
        response = await provider.send(request)

        self.assertEqual(response.text, "Hello")
        call_kwargs = mock_create.call_args.kwargs
        self.assertNotIn("previous_interaction_id", call_kwargs)
        self.assertIsInstance(call_kwargs["input"], list)

    async def test_stateful_second_send_chains_with_previous_interaction_id(self) -> None:
        provider = self._make_provider(session_id="s-chain", stateful=True)
        interaction = self._make_interaction(text="response")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request()
        await provider.send(request)
        await provider.send(request)

        second_call_kwargs = mock_create.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs["previous_interaction_id"], "interaction-abc")

    async def test_stateful_second_send_sends_only_delta(self) -> None:
        provider = self._make_provider(session_id="s-delta", stateful=True)
        interaction = self._make_interaction(text="first response")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        first_request = self._make_request()
        await provider.send(first_request)

        # Simulate runtime adding model response + new user turn
        second_request = self._make_request(
            messages=[
                LLMMessage(role="user", content=[LLMContentBlock.text("Hi")]),
                LLMMessage(role="assistant", content=[LLMContentBlock.text("first response")]),
                LLMMessage(role="user", content=[LLMContentBlock.text("Follow-up")]),
            ]
        )
        await provider.send(second_request)

        second_input = mock_create.call_args_list[1].kwargs["input"]
        # Delta should only include the new user turn (assistant is skipped)
        self.assertEqual(len(second_input), 1)
        self.assertEqual(second_input[0]["type"], "user_input")
        self.assertEqual(second_input[0]["content"][0]["text"], "Follow-up")

    async def test_stateless_mode_never_chains(self) -> None:
        # Default stateless provider must never use previous_interaction_id,
        # even across multiple sends in the same session.
        provider = self._make_provider(session_id="s-stateless")
        interaction = self._make_interaction(text="resp")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request()
        await provider.send(request)
        await provider.send(request)

        for call in mock_create.call_args_list:
            self.assertNotIn("previous_interaction_id", call.kwargs)

    async def test_stateless_mode_ignores_use_prompt_caching_flag(self) -> None:
        # GeminiProvider is stateless by default; use_prompt_caching on the
        # request has no effect — it is an Anthropic-specific concept.
        provider = self._make_provider(session_id="s-flag")
        interaction = self._make_interaction(text="resp")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request(use_prompt_caching=True)
        await provider.send(request)
        await provider.send(request)

        for call in mock_create.call_args_list:
            self.assertNotIn("previous_interaction_id", call.kwargs)

    async def test_structured_output_sets_response_format(self) -> None:
        provider = self._make_provider()
        interaction = self._make_interaction(text="{}")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        request = self._make_request(provider_options={"structured_output": schema})
        await provider.send(request)

        call_kwargs = mock_create.call_args.kwargs
        self.assertIn("response_format", call_kwargs)
        self.assertEqual(call_kwargs["response_format"]["type"], "text")
        self.assertEqual(call_kwargs["response_format"]["mime_type"], "application/json")
        self.assertIn("schema", call_kwargs["response_format"])

    async def test_system_instruction_passed_through(self) -> None:
        provider = self._make_provider()
        interaction = self._make_interaction(text="ok")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request(system="Custom system prompt.")
        await provider.send(request)

        call_kwargs = mock_create.call_args.kwargs
        self.assertEqual(call_kwargs["system_instruction"], "Custom system prompt.")

    async def test_capabilities_reports_structured_output(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        caps = provider.capabilities()
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.reasoning_content)
        self.assertTrue(caps.reasoning_controls)

    def test_parse_interaction_response_thought_step(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        from types import SimpleNamespace as _NS
        interaction = SimpleNamespace(
            id="i-1",
            status="completed",
            steps=[
                SimpleNamespace(type="user_input", content=[]),
                SimpleNamespace(
                    type="thought",
                    summary=[SimpleNamespace(type="text", text="I need to think...")],
                ),
                SimpleNamespace(
                    type="model_output",
                    content=[SimpleNamespace(type="text", text="Here is my answer.")],
                ),
            ],
            usage=SimpleNamespace(
                total_input_tokens=10, total_output_tokens=5, total_tokens=15,
                total_cached_tokens=None, total_thought_tokens=50,
            ),
        )
        parsed = provider._parse_interaction_response(interaction)

        self.assertEqual(len(parsed.content_blocks), 2)
        self.assertEqual(parsed.content_blocks[0].type, "thinking")
        self.assertEqual(parsed.content_blocks[0].data["thinking"], "I need to think...")
        self.assertEqual(parsed.content_blocks[1].type, "text")
        self.assertEqual(parsed.text, "Here is my answer.")
        self.assertEqual(parsed.usage.metadata["thought_tokens"], 50)

    async def test_stream_response_text(self) -> None:
        provider = self._make_provider()

        async def _fake_stream():
            yield SimpleNamespace(event_type="step.delta", index=0,
                delta=SimpleNamespace(type="text", text="Hello "))
            yield SimpleNamespace(event_type="step.delta", index=0,
                delta=SimpleNamespace(type="text", text="world"))
            yield SimpleNamespace(event_type="interaction.completed",
                interaction=SimpleNamespace(
                    id="i-stream-1", status="completed",
                    usage=SimpleNamespace(
                        total_input_tokens=10, total_output_tokens=5,
                        total_tokens=15, total_cached_tokens=None, total_thought_tokens=None,
                    ),
                ))

        mock_create = AsyncMock(return_value=_fake_stream())
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=mock_create))
        )

        request = self._make_request(streaming=True)
        response = await provider.send(request)

        self.assertEqual(response.text, "Hello world")
        self.assertEqual(response.stop_reason, "end_turn")

    async def test_stream_response_tool_call(self) -> None:
        provider = self._make_provider()

        async def _fake_stream():
            yield SimpleNamespace(
                event_type="step.start", index=1,
                step=SimpleNamespace(
                    type="function_call", id="call-s1", name="search",
                    arguments={"q": "cats"},
                ),
            )
            yield SimpleNamespace(event_type="interaction.completed",
                interaction=SimpleNamespace(
                    id="i-stream-2", status="requires_action",
                    usage=SimpleNamespace(
                        total_input_tokens=5, total_output_tokens=2,
                        total_tokens=7, total_cached_tokens=None, total_thought_tokens=None,
                    ),
                ))

        mock_create = AsyncMock(return_value=_fake_stream())
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=mock_create))
        )

        request = self._make_request(streaming=True)
        response = await provider.send(request)

        self.assertEqual(response.stop_reason, "tool_call")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "search")
        self.assertEqual(response.tool_calls[0].arguments, {"q": "cats"})

    async def test_stream_response_with_thought(self) -> None:
        provider = self._make_provider()

        async def _fake_stream():
            yield SimpleNamespace(event_type="step.delta", index=0,
                delta=SimpleNamespace(
                    type="thought_summary",
                    content=SimpleNamespace(type="text", text="thinking hard"),
                ))
            yield SimpleNamespace(event_type="step.delta", index=1,
                delta=SimpleNamespace(type="text", text="Done."))
            yield SimpleNamespace(event_type="interaction.completed",
                interaction=SimpleNamespace(
                    id="i-stream-3", status="completed",
                    usage=SimpleNamespace(
                        total_input_tokens=8, total_output_tokens=3,
                        total_tokens=11, total_cached_tokens=None, total_thought_tokens=20,
                    ),
                ))

        mock_create = AsyncMock(return_value=_fake_stream())
        provider._client = SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=mock_create))
        )

        request = self._make_request(streaming=True)
        response = await provider.send(request)

        self.assertEqual(response.text, "Done.")
        thinking_block = next(b for b in response.content_blocks if b.type == "thinking")
        self.assertEqual(thinking_block.data["thinking"], "thinking hard")
        self.assertEqual(response.usage.metadata["thought_tokens"], 20)

    def test_web_search_tools_replaced_with_native_google_search(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        tools = [
            LLMToolDefinition(name="web_search", description="Search", parameters_json_schema={"type": "object"}),
            LLMToolDefinition(name="web_fetch", description="Fetch", parameters_json_schema={"type": "object"}),
            LLMToolDefinition(name="bash", description="Run bash", parameters_json_schema={"type": "object"}),
        ]
        result = provider._build_interaction_tools(tools)

        # google_search appears exactly once
        native = [t for t in result if t.get("type") == "google_search"]
        self.assertEqual(len(native), 1)

        # web_search / web_fetch function declarations are gone
        names = [t.get("name") for t in result if t.get("type") == "function"]
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_fetch", names)

        # other function tools are preserved
        self.assertIn("bash", names)

    def test_only_web_search_tools_adds_native_only(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        tools = [
            LLMToolDefinition(name="web_search", description="Search", parameters_json_schema={"type": "object"}),
            LLMToolDefinition(name="web_fetch", description="Fetch", parameters_json_schema={"type": "object"}),
        ]
        result = provider._build_interaction_tools(tools)
        self.assertEqual(result, [{"type": "google_search"}])

    async def test_web_search_produces_no_function_call_round_trip(self) -> None:
        # With native google_search, the model returns text directly — no tool_call
        # step reaches the agent loop.
        provider = self._make_provider()
        interaction = self._make_interaction(text="Paris is the capital of France.")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request(
            messages=[LLMMessage(role="user", content=[LLMContentBlock.text("What is the capital of France?")])],
            tools=[
                LLMToolDefinition(name="web_search", description="Search", parameters_json_schema={"type": "object"}),
            ],
        )
        response = await provider.send(request)

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(response.tool_calls, [])
        sent_tools = mock_create.call_args.kwargs["tools"]
        self.assertEqual(sent_tools, [{"type": "google_search"}])

    # --- web_search constructor flag ---

    def test_web_search_flag_true_no_tools_adds_native_search(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = True
        result = provider._build_interaction_tools([])
        self.assertEqual(result, [{"type": "google_search"}])

    def test_web_search_flag_true_with_mcp_tools_deduplicates(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = True
        tools = [
            LLMToolDefinition(name="web_search", description="Search", parameters_json_schema={"type": "object"}),
            LLMToolDefinition(name="web_fetch", description="Fetch", parameters_json_schema={"type": "object"}),
            LLMToolDefinition(name="bash", description="Run bash", parameters_json_schema={"type": "object"}),
        ]
        result = provider._build_interaction_tools(tools)
        native = [t for t in result if t.get("type") == "google_search"]
        self.assertEqual(len(native), 1)
        names = [t.get("name") for t in result if t.get("type") == "function"]
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_fetch", names)
        self.assertIn("bash", names)

    def test_web_search_flag_false_unchanged_behavior(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        tools = [
            LLMToolDefinition(name="bash", description="Run bash", parameters_json_schema={"type": "object"}),
        ]
        result = provider._build_interaction_tools(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["name"], "bash")
        # no google_search when flag is off and no web tools present
        native = [t for t in result if t.get("type") == "google_search"]
        self.assertEqual(native, [])

    async def test_thinking_level_wired_into_generation_config(self) -> None:
        provider = self._make_provider()
        interaction = self._make_interaction(text="ok")
        client, mock_create = self._make_client(interaction)
        provider._client = client

        request = self._make_request(provider_options={"thinking_level": "high"})
        await provider.send(request)

        gen_config = mock_create.call_args.kwargs["generation_config"]
        self.assertEqual(gen_config["thinking_level"], "high")


class _FakeOSSStream:
    """Async-iterable stub for chat.completions.create(stream=True)."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return _aiter(self._chunks)


class OSSCompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    def _make_provider(self, **client):
        provider = object.__new__(OSSCompatibleProvider)
        provider._model = "qwen3"
        provider._app_id = "test"
        provider._default_provider_options = {}
        provider._on_tool_call_leak = "warn"
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(**client))
        )
        provider._emit_request_start = AsyncMock()
        provider._emit_request_complete = AsyncMock()
        provider._emit_request_error = AsyncMock()
        provider._emit_response_delta = AsyncMock()
        return provider

    def test_to_chat_messages_translates_transcript(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[
                LLMMessage(role="user", content=[LLMContentBlock.text("hi")]),
                LLMMessage(
                    role="assistant",
                    content=[
                        LLMContentBlock.text("calling"),
                        LLMContentBlock.tool_call(
                            tool_call_id="call-1",
                            name="lookup",
                            arguments={"q": "x"},
                        ),
                    ],
                ),
                LLMMessage(
                    role="tool",
                    content=[
                        LLMContentBlock.tool_result(
                            tool_call_id="call-1", content="result"
                        )
                    ],
                ),
            ],
            tools=[],
            max_tokens=100,
        )

        messages = provider._to_chat_messages(request, provider.capabilities())

        self.assertEqual(messages[0], {"role": "system", "content": "You are helpful."})
        self.assertEqual(messages[1], {"role": "user", "content": "hi"})
        assistant = messages[2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], "calling")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
            {"q": "x"},
        )
        self.assertEqual(
            messages[3],
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        )

    def test_to_chat_tools_uses_function_envelope(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        tools = [
            LLMToolDefinition(
                name="lookup",
                description="Look things up.",
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ]

        result = provider._to_chat_tools(tools)

        self.assertEqual(
            result,
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Look things up.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

    def test_parse_maps_tool_calls_and_usage(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="hello",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(
                                    name="lookup", arguments='{"query":"test"}'
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
        )

        parsed = provider._parse(raw, provider.capabilities())

        self.assertEqual(parsed.text, "hello")
        self.assertEqual(parsed.stop_reason, "tool_call")
        self.assertEqual(
            parsed.tool_calls,
            [ToolCall(id="call-1", name="lookup", arguments={"query": "test"})],
        )
        self.assertEqual(parsed.content_blocks[0].to_dict(), {"type": "text", "text": "hello"})
        self.assertEqual(parsed.content_blocks[1].to_dict()["type"], "tool_call")
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.input_tokens, 10)
        self.assertEqual(parsed.usage.cache_read_tokens, 2)

    def test_parse_maps_length_finish_reason_to_max_tokens(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="partial", tool_calls=None),
                    finish_reason="length",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, provider.capabilities())

        self.assertEqual(parsed.stop_reason, "max_tokens")
        self.assertEqual(parsed.text, "partial")

    def test_parse_arguments_is_tolerant_of_malformed_json(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        self.assertEqual(provider._parse_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(provider._parse_arguments("not json"), {})
        self.assertEqual(provider._parse_arguments(None), {})
        self.assertEqual(provider._parse_arguments({"a": 1}), {"a": 1})

    async def test_send_passes_tools_on_native_path(self) -> None:
        provider = self._make_provider(create=AsyncMock(return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )))
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[LLMMessage(role="user", content=[LLMContentBlock.text("hi")])],
            tools=[
                LLMToolDefinition(
                    name="lookup",
                    description="Look things up.",
                    parameters_json_schema={"type": "object"},
                )
            ],
            max_tokens=100,
        )

        response = await provider.send(request)

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "qwen3")
        self.assertEqual(call_kwargs["tools"][0]["function"]["name"], "lookup")
        self.assertNotIn("stream", call_kwargs)
        self.assertEqual(response.stop_reason, "end_turn")

    async def test_streamed_send_accumulates_content_and_tool_calls(self) -> None:
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="hello ", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="world", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    function=SimpleNamespace(name="lookup", arguments='{"q":'),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(name=None, arguments='"x"}'),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3, completion_tokens=4, total_tokens=7,
                    prompt_tokens_details=None,
                ),
            ),
        ]
        provider = self._make_provider(
            create=AsyncMock(return_value=_FakeOSSStream(chunks))
        )
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=100,
            streaming=True,
        )

        response = await provider.send(request)

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertTrue(call_kwargs["stream"])
        self.assertEqual(response.text, "hello world")
        self.assertEqual(
            response.tool_calls,
            [ToolCall(id="call-1", name="lookup", arguments={"q": "x"})],
        )
        self.assertEqual(response.stop_reason, "tool_call")
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 7)
        provider._emit_response_delta.assert_awaited()

    # -- Milestone 2: capability branches ------------------------------------

    @staticmethod
    def _ok_response():
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="{}", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    async def test_send_sets_response_format_when_structured_output_supported(self) -> None:
        provider = self._make_provider(create=AsyncMock(return_value=self._ok_response()))
        provider.capabilities = lambda: LLMCapabilities(
            streaming=True, native_tool_calling=True, structured_output=True
        )
        schema = {
            "type": "object",
            "title": "Result",
            "properties": {"ok": {"type": "boolean"}},
        }
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=50,
            provider_options={"structured_output": schema},
        )

        await provider.send(request)

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["response_format"],
            {
                "type": "json_schema",
                "json_schema": {"name": "Result", "schema": schema, "strict": True},
            },
        )
        # Schema is constrained server-side, not injected into the prompt.
        self.assertEqual(
            call_kwargs["messages"][0], {"role": "system", "content": "You are helpful."}
        )

    async def test_send_injects_schema_prompt_when_structured_output_unsupported(self) -> None:
        provider = self._make_provider(create=AsyncMock(return_value=self._ok_response()))
        provider.capabilities = lambda: LLMCapabilities(
            streaming=True, native_tool_calling=True, structured_output=False
        )
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        }
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=50,
            provider_options={"structured_output": schema},
        )

        await provider.send(request)

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertNotIn("response_format", call_kwargs)
        system_content = call_kwargs["messages"][0]["content"]
        self.assertIn("You are helpful.", system_content)
        self.assertIn("JSON Schema", system_content)
        self.assertIn('"ok"', system_content)

    def test_parse_splits_reasoning_content_field(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="final answer",
                        reasoning_content="step by step",
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, LLMCapabilities(reasoning_content=True))

        self.assertEqual(parsed.text, "final answer")
        self.assertEqual(parsed.provider_metadata["reasoning"], "step by step")

    def test_parse_strips_inline_think_block(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<think>secret</think>visible", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, LLMCapabilities(reasoning_content=True))

        self.assertEqual(parsed.text, "visible")
        self.assertEqual(parsed.provider_metadata["reasoning"], "secret")

    def test_parse_splits_openrouter_reasoning_field(self) -> None:
        # OpenRouter normalizes thinking into a `reasoning` field (not
        # `reasoning_content`); it must be split out the same way.
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="visible", reasoning="step by step", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, LLMCapabilities(reasoning_content=True))

        self.assertEqual(parsed.text, "visible")
        self.assertEqual(parsed.provider_metadata["reasoning"], "step by step")

    def test_parse_keeps_reasoning_inline_when_capability_off(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<think>x</think>hello", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, LLMCapabilities(reasoning_content=False))

        self.assertEqual(parsed.text, "<think>x</think>hello")
        self.assertNotIn("reasoning", parsed.provider_metadata)

    def test_gemma_capabilities_split_inline_think_block(self) -> None:
        # Gemma enables reasoning_content so its inline <think> block is split
        # out of the transcript instead of leaking into the answer.
        provider = object.__new__(GemmaProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<think>plan</think>answer", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        self.assertTrue(provider.capabilities().reasoning_content)
        parsed = provider._parse(raw, provider.capabilities())

        self.assertEqual(parsed.text, "answer")
        self.assertEqual(parsed.provider_metadata["reasoning"], "plan")

    def test_gemma_capabilities_leave_plain_content_unchanged(self) -> None:
        # With reasoning off, a non-reasoning response has no <think> block; the
        # split is a no-op and the text passes through untouched.
        provider = object.__new__(GemmaProvider)
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="just answer", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

        parsed = provider._parse(raw, provider.capabilities())

        self.assertEqual(parsed.text, "just answer")
        self.assertNotIn("reasoning", parsed.provider_metadata)

    async def test_streamed_send_accumulates_reasoning_content(self) -> None:
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="think ", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="more", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="answer", reasoning_content=None, tool_calls=None), finish_reason="stop")],
                usage=None,
            ),
        ]
        provider = self._make_provider(
            create=AsyncMock(return_value=_FakeOSSStream(chunks))
        )
        provider.capabilities = lambda: LLMCapabilities(
            streaming=True, native_tool_calling=True, reasoning_content=True
        )
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=50,
            streaming=True,
        )

        response = await provider.send(request)

        self.assertEqual(response.text, "answer")
        self.assertEqual(response.provider_metadata["reasoning"], "think more")

    async def test_streamed_send_accumulates_openrouter_reasoning_field(self) -> None:
        # Streamed deltas may carry thinking under OpenRouter's `reasoning` name.
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning="think ", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning="more", tool_calls=None), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="answer", reasoning=None, tool_calls=None), finish_reason="stop")],
                usage=None,
            ),
        ]
        provider = self._make_provider(
            create=AsyncMock(return_value=_FakeOSSStream(chunks))
        )
        provider.capabilities = lambda: LLMCapabilities(
            streaming=True, native_tool_calling=True, reasoning_content=True
        )
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=50,
            streaming=True,
        )

        response = await provider.send(request)

        self.assertEqual(response.text, "answer")
        self.assertEqual(response.provider_metadata["reasoning"], "think more")

    async def test_send_merges_default_provider_options_request_wins(self) -> None:
        # Construction-time defaults pass through, but a per-request option of
        # the same name overrides the default.
        provider = self._make_provider(
            create=AsyncMock(return_value=self._ok_response())
        )
        provider._default_provider_options = {
            "extra_body": {"provider": {"require_parameters": True}},
            "top_p": 0.5,
        }
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[],
            max_tokens=50,
            provider_options={"top_p": 0.9},
        )

        await provider.send(request)

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["top_p"], 0.9)  # request wins
        self.assertEqual(
            kwargs["extra_body"], {"provider": {"require_parameters": True}}
        )  # default passes through untouched

    def _leaked_response(self):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='<|tool_call>call:AskUser{"q": 1}<tool_call|>',
                        tool_calls=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    def _tools_request(self):
        return LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[
                LLMToolDefinition(
                    name="AskUser",
                    description="Ask the user.",
                    parameters_json_schema={"type": "object"},
                )
            ],
            max_tokens=50,
        )

    def test_parse_warns_on_leaked_tool_call(self) -> None:
        provider = self._make_provider()
        provider._on_tool_call_leak = "warn"
        caps = LLMCapabilities(native_tool_calling=True)

        with self.assertLogs("mash.core.llm.oss", level="WARNING") as cm:
            parsed = provider._parse(
                self._leaked_response(), caps, self._tools_request()
            )

        self.assertTrue(parsed.provider_metadata["tool_call_leak"])
        self.assertEqual(parsed.tool_calls, [])
        self.assertEqual(parsed.stop_reason, "end_turn")
        self.assertTrue(any("tool-call parser" in line for line in cm.output))

    def test_parse_raises_on_leaked_tool_call_when_strict(self) -> None:
        provider = self._make_provider()
        provider._on_tool_call_leak = "raise"
        caps = LLMCapabilities(native_tool_calling=True)

        with self.assertRaises(ValueError):
            provider._parse(self._leaked_response(), caps, self._tools_request())

    def test_parse_ignore_suppresses_leak_warning(self) -> None:
        provider = self._make_provider()
        provider._on_tool_call_leak = "ignore"
        caps = LLMCapabilities(native_tool_calling=True)

        with self.assertNoLogs("mash.core.llm.oss", level="WARNING"):
            parsed = provider._parse(
                self._leaked_response(), caps, self._tools_request()
            )

        # Detection still recorded for traces; just neither logged nor raised.
        self.assertTrue(parsed.provider_metadata["tool_call_leak"])

    def test_parse_no_leak_when_tool_calls_present(self) -> None:
        # Happy path: structured tool_calls returned, so nothing is flagged.
        provider = self._make_provider()
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="c1",
                                type="function",
                                function=SimpleNamespace(
                                    name="AskUser", arguments="{}"
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )
        caps = LLMCapabilities(native_tool_calling=True)

        with self.assertNoLogs("mash.core.llm.oss", level="WARNING"):
            parsed = provider._parse(raw, caps, self._tools_request())

        self.assertNotIn("tool_call_leak", parsed.provider_metadata)
        self.assertEqual(parsed.stop_reason, "tool_call")

    def test_parse_no_leak_for_plain_text_answer(self) -> None:
        # Tools sent, no tool_calls, but ordinary prose must not false-positive.
        provider = self._make_provider()
        raw = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Here is your answer.", tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        caps = LLMCapabilities(native_tool_calling=True)

        with self.assertNoLogs("mash.core.llm.oss", level="WARNING"):
            parsed = provider._parse(raw, caps, self._tools_request())

        self.assertNotIn("tool_call_leak", parsed.provider_metadata)
        self.assertEqual(parsed.stop_reason, "end_turn")

    def test_constructor_rejects_invalid_leak_action(self) -> None:
        with self.assertRaises(ValueError):
            OSSCompatibleProvider(
                app_id="t", model="qwen3", on_tool_call_leak="bogus"
            )

    async def test_send_rejects_tools_without_native_tool_calling(self) -> None:
        provider = self._make_provider(create=AsyncMock(return_value=self._ok_response()))
        provider.capabilities = lambda: LLMCapabilities(
            streaming=True, native_tool_calling=False
        )
        request = LLMRequest(
            model="qwen3",
            system="You are helpful.",
            messages=[],
            tools=[
                LLMToolDefinition(
                    name="lookup",
                    description="Look things up.",
                    parameters_json_schema={"type": "object"},
                )
            ],
            max_tokens=50,
        )

        with self.assertRaises(ValueError):
            await provider.send(request)
        provider._client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()

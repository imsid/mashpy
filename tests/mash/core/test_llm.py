"""Tests for provider-neutral LLM contracts and adapters."""

from __future__ import annotations

import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from mash.core.context import ToolCall
from mash.core.llm import AnthropicProvider, OpenAIProvider
from mash.core.llm.types import (
    LLMContentBlock,
    LLMMessage,
    LLMRequest,
    LLMToolDefinition,
)


class LLMProviderContractTests(unittest.TestCase):
    def test_openai_send_omits_temperature_for_gpt5_models(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-5"
        provider._app_id = "test"
        provider._client = SimpleNamespace(responses=SimpleNamespace(create=Mock()))
        provider._emit_request_start = Mock()
        provider._emit_request_complete = Mock()
        provider._emit_request_error = Mock()
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

        provider.send(request)

        call_kwargs = provider._client.responses.create.call_args.kwargs
        self.assertNotIn("temperature", call_kwargs)

    def test_openai_send_keeps_temperature_for_non_gpt5_models(self) -> None:
        provider = object.__new__(OpenAIProvider)
        provider._model = "gpt-4.1"
        provider._app_id = "test"
        provider._client = SimpleNamespace(responses=SimpleNamespace(create=Mock()))
        provider._emit_request_start = Mock()
        provider._emit_request_complete = Mock()
        provider._emit_request_error = Mock()
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

        provider.send(request)

        call_kwargs = provider._client.responses.create.call_args.kwargs
        self.assertEqual(call_kwargs["temperature"], 0.2)

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


if __name__ == "__main__":
    unittest.main()

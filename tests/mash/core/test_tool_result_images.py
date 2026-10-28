"""Tool-result images: through the agent's context and into each provider."""

from __future__ import annotations

import unittest

from mash.core.context import Context, MessageRole, ToolResult
from mash.core.llm import AnthropicProvider, GeminiProvider, OpenAIProvider
from mash.core.llm.oss import OSSCompatibleProvider
from mash.core.llm.types import LLMCapabilities, LLMContentBlock, LLMMessage, LLMRequest

IMAGE = {"media_type": "image/png", "data": "iVBORw0KGgo="}


def _request() -> LLMRequest:
    return LLMRequest(
        model="m",
        system="",
        messages=[
            LLMMessage(
                role="assistant",
                content=[LLMContentBlock.tool_call(tool_call_id="c-1", name="shot", arguments={})],
            ),
            LLMMessage(
                role="tool",
                content=[
                    LLMContentBlock.tool_result(
                        tool_call_id="c-1", content="page", images=[IMAGE]
                    )
                ],
            ),
        ],
        tools=[],
        max_tokens=100,
    )


class ProviderImageTests(unittest.TestCase):
    def test_anthropic_puts_images_inside_the_tool_result(self) -> None:
        provider = object.__new__(AnthropicProvider)
        [_, result] = provider._anthropic_messages(_request())

        block = result["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["content"][0], {"type": "text", "text": "page"})
        self.assertEqual(
            block["content"][1],
            {"type": "image", "source": {"type": "base64", **IMAGE}},
        )

    def test_openai_returns_input_images_in_the_function_output(self) -> None:
        provider = object.__new__(OpenAIProvider)
        items = provider._openai_input(_request())

        output = items[-1]["output"]
        self.assertEqual(output[0], {"type": "input_text", "text": "page"})
        self.assertEqual(
            output[1], {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}
        )

    def test_gemini_follows_the_results_with_a_user_step_of_images(self) -> None:
        provider = object.__new__(GeminiProvider)
        provider._web_search = False
        steps = provider._messages_to_steps(_request().messages, {"c-1": "shot"})

        self.assertEqual(steps[-2]["type"], "function_result")
        self.assertEqual(steps[-1]["type"], "user_input")
        self.assertEqual(
            steps[-1]["content"][1],
            {"type": "image", "data": "iVBORw0KGgo=", "mime_type": "image/png"},
        )

    def test_chat_completions_follow_the_tool_messages_with_a_user_message(self) -> None:
        provider = object.__new__(OSSCompatibleProvider)
        messages = provider._to_chat_messages(_request(), LLMCapabilities())

        self.assertEqual(messages[-2]["role"], "tool")
        self.assertEqual(messages[-2]["content"], "page")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(
            messages[-1]["content"][1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        )

    def test_results_without_images_are_unchanged(self) -> None:
        request = _request()
        request.messages[1].content[0].data.pop("images")
        provider = object.__new__(AnthropicProvider)
        self.assertEqual(provider._anthropic_messages(request)[1]["content"][0]["content"], "page")


class ContextImageTests(unittest.TestCase):
    def test_tool_results_keep_their_images_as_llm_blocks(self) -> None:
        context = Context()
        context.add_message(
            MessageRole.TOOL,
            [ToolResult(tool_call_id="c-1", content="page", images=[IMAGE]).to_dict()],
        )
        [message] = context.get_messages_for_llm()
        self.assertEqual(message.content[0].data["images"], [IMAGE])

    def test_only_the_most_recent_images_are_kept(self) -> None:
        context = Context()
        for n in range(4):
            image = {"media_type": "image/png", "data": f"img{n}"}
            context.add_message(
                MessageRole.TOOL,
                [ToolResult(tool_call_id=f"c-{n}", content=f"shot {n}", images=[image]).to_dict()],
            )

        context.drop_old_images(3)

        blocks = [m.content[0] for m in context.messages]
        self.assertNotIn("images", blocks[0])
        self.assertEqual(blocks[0]["content"], "shot 0\n[older image omitted]")
        self.assertEqual([b["images"][0]["data"] for b in blocks[1:]], ["img1", "img2", "img3"])


if __name__ == "__main__":
    unittest.main()

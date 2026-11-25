"""Large tool results: the model sees a preview, the event log keeps the result."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.runtime import AgentRuntime, AgentSpec
from mash.runtime.events import RuntimeEventType
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import TEST_IDENTITY
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry
from mash.tools.tool_results import PREVIEW_CHARS, ReadToolResultTool, context_content

BIG = "".join(f"{n:05d}|" for n in range(6000))  # 36,000 chars, each position readable


class _BigTool:
    name = "fetch"
    description = "Fetch a page."
    requires_approval = False
    parallel_safe = True
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        return ToolResult.success(BIG)

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class _Provider(LLMProvider):
    """Calls fetch, then answers; records what each call saw of the result."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        results = [
            block.data.get("content")
            for message in request.messages
            for block in message.content
            if block.type == "tool_result"
        ]
        usage = LLMTokenUsage(input_tokens=5, output_tokens=1, total_tokens=6)
        if not results:
            call = ToolCall(id="call-big", name="fetch", arguments={})
            return LLMResponse(
                thought_summary="",
                text="",
                tool_calls=[call],
                content_blocks=[
                    LLMContentBlock.tool_call(tool_call_id="call-big", name="fetch", arguments={})
                ],
                stop_reason="tool_call",
                usage=usage,
            )
        self.seen.append(str(results[-1]))
        return LLMResponse(
            thought_summary="",
            text="read it",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("read it")],
            stop_reason="end_turn",
            usage=usage,
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class _Spec(AgentSpec):
    def __init__(self) -> None:
        self.provider = _Provider()

    def get_agent_id(self) -> str:
        return "big-app"

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(_BigTool())
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(agent_id="big-app", system_prompt="You are big-app.")


class ContextContentTests(unittest.TestCase):
    def test_a_short_result_is_shown_whole(self) -> None:
        self.assertEqual(context_content("short", limit=20_000, trace_id="t", tool_call_id="c"), "short")
        exact = "x" * 20_000
        self.assertEqual(context_content(exact, limit=20_000, trace_id="t", tool_call_id="c"), exact)

    def test_a_long_result_is_a_preview_that_says_how_to_read_on(self) -> None:
        shown = context_content(BIG, limit=20_000, trace_id="t-1", tool_call_id="call-1")
        header, body = shown.split("\n", 1)
        self.assertEqual(body, BIG[:PREVIEW_CHARS])
        self.assertIn("36,000 chars", header)
        self.assertIn(f"chars 0-{PREVIEW_CHARS:,}", header)
        self.assertIn(
            f'ReadToolResult(trace_id="t-1", tool_call_id="call-1", offset={PREVIEW_CHARS})',
            header,
        )


class LargeResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (
            patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name}),
            patch(
                "mash.runtime.service.resolve_database_url",
                return_value="postgresql://test/runtime",
            ),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.runtime_store, self.memory_store = build_test_stores()

    async def _run(self) -> tuple[AgentRuntime, _Spec, str, str]:
        spec = _Spec()
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="s-1",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.engine.close)
        accepted = await runtime.submit_request(message="go", session_id="s-1", **TEST_IDENTITY)
        request_id = accepted["request_id"]
        for _ in range(500):
            events = await self.runtime_store.list_request_events(request_id)
            end = [e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_END.value]
            if end:
                return runtime, spec, request_id, str(end[0].trace_id)
            await asyncio.sleep(0.01)
        self.fail("the request never ended")

    async def test_the_model_sees_a_preview_and_the_log_keeps_the_result(self) -> None:
        _, spec, request_id, trace_id = await self._run()
        [seen] = spec.provider.seen
        self.assertEqual(
            seen,
            context_content(BIG, limit=20_000, trace_id=trace_id, tool_call_id="call-big"),
        )
        [end] = [
            e
            for e in await self.runtime_store.list_request_events(request_id)
            if e.event_type == RuntimeEventType.EXECUTE_TOOL_END.value
        ]
        self.assertEqual(end.payload["result"]["content"], BIG)

    async def test_read_tool_result_reads_on_from_the_log(self) -> None:
        _, _, _, trace_id = await self._run()
        tool = ReadToolResultTool(
            runtime_store=self.runtime_store,
            agent_id="big-app",
            session_id="s-1",
            context_chars=20_000,
        )
        part = await tool.execute(
            {"trace_id": trace_id, "tool_call_id": "call-big", "offset": PREVIEW_CHARS, "limit": 600}
        )
        self.assertFalse(part.is_error)
        header, body = part.content.split("\n", 1)
        self.assertEqual(body, BIG[PREVIEW_CHARS : PREVIEW_CHARS + 600])
        self.assertIn(f"of 36,000; read more with offset={PREVIEW_CHARS + 600}", header)

        last = await tool.execute(
            {"trace_id": trace_id, "tool_call_id": "call-big", "offset": 35_900}
        )
        self.assertTrue(last.content.split("\n", 1)[0].endswith("this is the end]"))
        self.assertEqual(last.content.split("\n", 1)[1], BIG[35_900:])

        # The default and the most it returns leave room for its header, so its
        # own result is never previewed.
        whole = await tool.execute({"trace_id": trace_id, "tool_call_id": "call-big"})
        self.assertLessEqual(len(whole.content), 20_000)

        for args, message in (
            ({"trace_id": trace_id, "tool_call_id": "nope"}, "no result of call"),
            ({"trace_id": trace_id, "tool_call_id": "call-big", "offset": 40_000}, "past the end"),
            ({"trace_id": trace_id, "tool_call_id": "call-big", "limit": 50_000}, "at most"),
        ):
            with self.subTest(args=args):
                refused = await tool.execute(args)
                self.assertTrue(refused.is_error)
                self.assertIn(message, refused.content)

    async def test_read_tool_result_reads_only_its_own_session(self) -> None:
        _, _, _, trace_id = await self._run()
        other = ReadToolResultTool(
            runtime_store=self.runtime_store,
            agent_id="big-app",
            session_id="s-other",
            context_chars=20_000,
        )
        refused = await other.execute({"trace_id": trace_id, "tool_call_id": "call-big"})
        self.assertTrue(refused.is_error)


if __name__ == "__main__":
    unittest.main()

"""Large tool results: saved to a file in the sandbox, the event log keeps the result."""

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
from mash.runtime import factory
from mash.tools.tool_results import SAVED_TO, context_content, result_file_path

BIG = "".join(f"{n:05d}\n" for n in range(6000))  # 36,000 chars in 6,000 lines


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
        saved = {SAVED_TO: "tool-results/t/c.txt"}
        self.assertEqual(context_content("short", limit=20_000, metadata=saved), "short")
        exact = "x" * 20_000
        self.assertEqual(context_content(exact, limit=20_000, metadata=saved), exact)

    def test_a_long_saved_result_is_a_note_naming_its_file(self) -> None:
        note = context_content(
            BIG, limit=20_000, metadata={SAVED_TO: "tool-results/t-1/call-1.txt", "exit_code": 1}
        )
        self.assertNotIn("00001", note)
        self.assertIn("36,000 characters in 6,000 lines, exit code 1", note)
        self.assertIn("saved whole to tool-results/t-1/call-1.txt", note)
        self.assertIn("tail -n 50 tool-results/t-1/call-1.txt", note)

    def test_a_long_result_not_saved_is_shown_whole(self) -> None:
        self.assertEqual(context_content(BIG, limit=20_000, metadata={}), BIG)
        self.assertEqual(context_content(BIG, limit=20_000, metadata=None), BIG)

    def test_the_path_comes_from_the_ids(self) -> None:
        self.assertEqual(result_file_path("abc123", "call-1"), "tool-results/abc123/call-1.txt")
        self.assertEqual(
            result_file_path("abc", "toolu/../x y"), "tool-results/abc/toolu_.._x_y.txt"
        )

    def test_a_request_has_a_sandbox_unless_the_host_or_policy_says_no(self) -> None:
        runtime = type("R", (), {"_sandbox_tools": ("http://host", "tok")})()
        self.assertEqual(factory.request_sandbox(runtime, None), ("http://host", "tok"))
        self.assertEqual(factory.request_sandbox(runtime, {"sandbox": True}), ("http://host", "tok"))
        self.assertIsNone(factory.request_sandbox(runtime, {"sandbox": False}))
        runtime._sandbox_tools = None
        self.assertIsNone(factory.request_sandbox(runtime, None))


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
        self.written: list[dict[str, Any]] = []

    def _sandbox(self, *, fail: bool = False) -> None:
        async def write(**kwargs: Any) -> None:
            if fail:
                raise RuntimeError("Sandbox error: no sandbox")
            self.written.append(kwargs)

        for target in (
            patch("mash.runtime.factory.request_sandbox", return_value=("http://host", "tok")),
            patch("mash.runtime.engine.steps.write_sandbox_file", side_effect=write),
        ):
            target.start()
            self.addCleanup(target.stop)

    async def _run(self) -> tuple[_Spec, str, str]:
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
                return spec, request_id, str(end[0].trace_id)
            await asyncio.sleep(0.01)
        self.fail("the request never ended")

    async def _tool_end(self, request_id: str) -> Any:
        [end] = [
            e
            for e in await self.runtime_store.list_request_events(request_id)
            if e.event_type == RuntimeEventType.EXECUTE_TOOL_END.value
        ]
        return end

    async def test_a_long_result_is_saved_to_the_sandbox_and_the_model_sees_a_note(self) -> None:
        self._sandbox()
        spec, request_id, trace_id = await self._run()

        path = result_file_path(trace_id, "call-big")
        [written] = self.written
        self.assertEqual(
            written,
            {
                "host_api_url": "http://host",
                "token": "tok",
                "request_id": request_id,
                "path": path,
                "data": BIG.encode(),
            },
        )
        [seen] = spec.provider.seen
        self.assertEqual(seen, context_content(BIG, limit=20_000, metadata={SAVED_TO: path}))
        end = await self._tool_end(request_id)
        self.assertEqual(end.payload["result"]["content"], BIG)
        self.assertEqual(end.payload["result"]["metadata"][SAVED_TO], path)

    async def test_without_a_sandbox_the_model_gets_the_whole_result(self) -> None:
        spec, request_id, _ = await self._run()

        self.assertEqual(spec.provider.seen, [BIG])
        end = await self._tool_end(request_id)
        self.assertNotIn(SAVED_TO, end.payload["result"]["metadata"])

    async def test_a_failed_write_leaves_the_whole_result(self) -> None:
        self._sandbox(fail=True)
        spec, request_id, _ = await self._run()

        self.assertEqual(spec.provider.seen, [BIG])
        end = await self._tool_end(request_id)
        self.assertNotIn(SAVED_TO, end.payload["result"]["metadata"])


if __name__ == "__main__":
    unittest.main()

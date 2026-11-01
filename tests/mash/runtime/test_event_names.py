"""A request writes only the OpenTelemetry-named events, in span order."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.otel.projection import project_request
from mash.runtime import AgentRuntime
from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import to_public_event
from mash.testing.runtime_fixtures import ToolCallingAgentSpec
from mash.tools.base import FunctionTool, ToolResult
from mash.tools.registry import ToolRegistry


class _EchoSpec(ToolCallingAgentSpec):
    def build_tools(self) -> ToolRegistry:
        async def echo(args) -> ToolResult:
            return ToolResult.success(f"echo {args.get('text')}")

        tools = ToolRegistry()
        tools.register(
            FunctionTool(
                name="echo",
                description="Echo text.",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                _executor=echo,
            )
        )
        return tools


class EventNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.runtime_store, memory_store = build_test_stores()
        self.runtime = AgentRuntime.from_spec(
            _EchoSpec(agent_id="writer", tool_name="echo", arguments={"text": "hi"}),
            session_id="host-session",
            runtime_store=self.runtime_store,
            memory_store=memory_store,
        )
        await self.runtime.open()
        self.addAsyncCleanup(self.runtime.shutdown)

    async def _run(self) -> list:
        accepted = await self.runtime.submit_request(
            message="say hi", session_id="s-1", user_id="u-1", request_id="req-1"
        )
        for _ in range(200):
            if await self.runtime_store.is_request_terminal(accepted["request_id"]):
                break
            await asyncio.sleep(0.01)
        return await self.runtime_store.list_request_events("req-1")

    async def test_a_tool_calling_request_writes_one_event_per_span_boundary(self) -> None:
        events = await self._run()

        self.assertEqual(
            [e.event_type for e in events],
            [
                "invoke_agent.start",
                "mash.context_load.start",
                "mash.context_load.end",
                "chat.start",
                "chat.end",
                "execute_tool.start",
                "execute_tool.end",
                "mash.step.end",
                "chat.start",
                "chat.end",
                "mash.step.end",
                "invoke_agent.end",
            ],
        )
        names = {t.value for t in RuntimeEventType}
        self.assertTrue(all(e.event_type in names for e in events))
        # Every event of the request belongs to its trace, the first one too.
        self.assertEqual(len({e.trace_id for e in events}), 1)
        self.assertIsNotNone(events[0].trace_id)

    async def test_events_carry_their_attributes_and_the_user_message_once(self) -> None:
        events = await self._run()
        by_type: dict[str, list] = {}
        for event in events:
            by_type.setdefault(event.event_type, []).append(event)

        messages = [e for e in events if (e.payload or {}).get("message") == "say hi"]
        self.assertEqual([e.event_type for e in messages], ["invoke_agent.start"])

        chat_end = by_type["chat.end"][0].payload
        self.assertEqual(chat_end["status"], "ok")
        self.assertEqual(chat_end["attributes"]["gen_ai.operation.name"], "chat")
        self.assertEqual(chat_end["attributes"]["gen_ai.request.model"], "test-model")
        self.assertEqual(chat_end["attributes"]["gen_ai.usage.input_tokens"], 2)
        self.assertNotIn("token_usage", chat_end)

        tool_start = by_type["execute_tool.start"][0].payload
        self.assertEqual(tool_start["arguments"], {"text": "hi"})
        self.assertEqual(tool_start["attributes"]["gen_ai.tool.name"], "echo")
        tool_end = by_type["execute_tool.end"][0].payload
        self.assertEqual(tool_end["status"], "ok")
        self.assertEqual(tool_end["result"]["content"], "echo hi")

        end = by_type["invoke_agent.end"][0]
        self.assertEqual(end.payload["status"], "ok")
        self.assertEqual(to_public_event(end)["event"], "request.completed")

    async def test_the_projection_of_a_real_request_is_one_closed_tree(self) -> None:
        events = await self._run()
        projection = project_request(events, capture_content=False)
        root = projection.span(projection.root_span_id)
        self.assertEqual(root.name, "invoke_agent writer")
        self.assertTrue(all(span.end_time is not None for span in projection.spans))
        self.assertEqual(
            sorted(s.name for s in projection.spans),
            sorted(
                [
                    "invoke_agent writer",
                    "mash.cold_start",
                    "mash.context_load",
                    "mash.step 0",
                    "mash.step 1",
                    "chat test-model",
                    "chat test-model",
                    "execute_tool echo",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()

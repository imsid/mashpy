"""The companion forwards an agent's answer unchanged with ForwardAnswer."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from mash.agents.companion import COMPANION_AGENT_ID, CompanionSpec
from mash.agents.companion.logs import AgentLogReader, GetAgentTurnsTool, InspectAgentTraceTool
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMResponse, LLMTokenUsage
from mash.logging import bound_session_id
from mash.runtime import Host, HostBuilder
from mash.runtime.engine.rebuild import rebuild_request_state
from mash.runtime.events import RuntimeEventType
from mash.testing.runtime_fixtures import build_spec, metadata

LONG = "".join(f"line {n:05d}\n" for n in range(3000))  # 33,000 chars


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    blocks = [LLMContentBlock.text(text)] if text else []
    blocks += [
        LLMContentBlock.tool_call(tool_call_id=c.id, name=c.name, arguments=c.arguments)
        for c in calls or []
    ]
    return LLMResponse(
        thought_summary="",
        text=text,
        tool_calls=list(calls or []),
        content_blocks=blocks,
        stop_reason="tool_call" if calls else "end_turn",
        usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Router(LLMProvider):
    """Routes 'agent|prompt' with RunSubAgents, then acts on the result per ``plan``.

    - forward: ForwardAnswer with the child's request_id
    - unknown: ForwardAnswer with an id that is not in the results
    - alongside: ForwardAnswer together with another call
    After a refused ForwardAnswer it writes its own reply.
    """

    def __init__(self, plan: str) -> None:
        self.plan = plan
        self.calls = 0
        self.seen_results: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request):
        self.calls += 1
        results = [
            block for m in request.messages for block in m.content if block.type == "tool_result"
        ]
        if not results:
            last = request.messages[-1]
            text = "".join(b.data.get("text", "") for b in last.content if b.type == "text")
            agent_id, prompt = text.rsplit("</routes>\n\n", 1)[-1].split("|", 1)
            return _response(
                calls=[
                    ToolCall(
                        id="call-run",
                        name="RunSubAgents",
                        arguments={"requests": [{"agent_id": agent_id, "prompt": prompt}]},
                    )
                ]
            )
        last = results[-1]
        if last.data.get("is_error"):
            self.seen_results.append(str(last.data.get("content")))
            return _response("my own reply")
        content = str(last.data.get("content"))
        self.seen_results.append(content)
        # The id is near the start of the entry, so a preview holds it too.
        request_id = re.search(r'"request_id": "([^"]+)"', content).group(1)
        forward = ToolCall(
            id="call-forward",
            name="ForwardAnswer",
            arguments={"request_id": "nope" if self.plan == "unknown" else request_id},
        )
        calls = [forward]
        if self.plan == "alongside":
            calls.append(ToolCall(id="call-other", name="ReadToolResult", arguments={}))
        return _response(calls=calls)

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def _pool(self, plan: str, *, response_text: str = "built it") -> _Router:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name, "MASH_DATABASE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        router = _Router(plan)
        llm = patch.object(CompanionSpec, "build_llm", lambda self: router)
        llm.start()
        self.addCleanup(llm.stop)
        self.pool = (
            HostBuilder()
            .agent(
                build_spec(agent_id="builder", response_text=response_text, fail_on_message="explode"),
                metadata=metadata(),
            )
            .host(Host(host_id="guide", agents=("builder",)))
            .build()
        )
        self.pool.configure_runtime_database_url("postgresql://test/runtime")
        await self.pool.start()
        self.addAsyncCleanup(self.pool.close)
        self.companion = self.pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
        return router

    async def _ask(self, message: str, session_id: str = "s-1") -> dict[str, Any]:
        accepted = await self.pool.submit_host_request(
            "guide", message=message, session_id=session_id, app_id="app-1", policy_id="p-1"
        )
        async for event in self.pool.get_client(COMPANION_AGENT_ID).stream_response(
            accepted["request_id"], timeout=10
        ):
            if event.get("event") in {"request.completed", "request.error"}:
                self.assertEqual(event["event"], "request.completed", event)
                return {**event["data"], "request_id": accepted["request_id"]}
        self.fail("no terminal event")

    async def _tool_end(self, request_id: str, name: str) -> Any:
        events = await self.companion.runtime_store.list_request_events(request_id)
        return next(
            e
            for e in events
            if e.event_type == RuntimeEventType.EXECUTE_TOOL_END.value
            and e.payload["attributes"]["gen_ai.tool.name"] == name
        )

    async def test_a_forwarded_answer_is_the_reply_with_no_further_model_call(self) -> None:
        router = await self._pool("forward")
        done = await self._ask("builder|scaffold rss")

        self.assertEqual(done["response"]["text"], "built it")
        # One call to route, one to forward: no call writes a reply.
        self.assertEqual(router.calls, 2)
        [turn] = await self.companion.store.get_turns("s-1", COMPANION_AGENT_ID)
        self.assertEqual(turn["agent_response"], "built it")
        [route] = turn["metadata"]["routes"]
        self.assertEqual((route["agent_id"], route["status"]), ("builder", "completed"))
        forward = await self._tool_end(done["request_id"], "ForwardAnswer")
        self.assertEqual(forward.payload["reply"], "built it")
        self.assertFalse(forward.payload["result"]["is_error"])

        # The rebuild from the events reaches the same end.
        events = await self.companion.runtime_store.list_request_events(done["request_id"])
        start = next(e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value)
        rebuilt = await rebuild_request_state(
            self.companion,
            session_id="s-1",
            message="builder|scaffold rss",
            request_metadata=dict(start.payload["request_metadata"]),
            events=events,
        )
        self.assertTrue(rebuilt["done"])
        self.assertEqual(
            rebuilt["context"]["messages"][-1]["content"], [{"type": "text", "text": "built it"}]
        )

    async def test_a_long_answer_forwards_whole_though_the_companion_saw_a_preview(self) -> None:
        router = await self._pool("forward", response_text=LONG)
        done = await self._ask("builder|write it all")

        self.assertTrue(router.seen_results[0].startswith("[Result is "))
        self.assertEqual(done["response"]["text"], LONG.strip())

    async def test_a_refused_forward_is_an_error_and_the_companion_goes_on(self) -> None:
        for plan, message in (
            ("unknown", "no RunSubAgents entry with request_id 'nope'"),
            ("alongside", "must be the only tool call in its step"),
        ):
            with self.subTest(plan=plan):
                router = await self._pool(plan)
                done = await self._ask("builder|scaffold rss", session_id=f"s-{plan}")

                self.assertEqual(done["response"]["text"], "my own reply")
                self.assertEqual(router.calls, 3)
                forward = await self._tool_end(done["request_id"], "ForwardAnswer")
                self.assertTrue(forward.payload["result"]["is_error"])
                self.assertIn(message, forward.payload["result"]["content"])
                self.assertNotIn("reply", forward.payload)
                await self.pool.close()

    async def test_a_failed_child_is_not_forwarded(self) -> None:
        await self._pool("forward")
        done = await self._ask("builder|explode now")

        self.assertEqual(done["response"]["text"], "my own reply")
        forward = await self._tool_end(done["request_id"], "ForwardAnswer")
        self.assertIn("not completed", forward.payload["result"]["content"])


class TranscriptViewTests(ForwardTests):
    async def test_the_transcript_view_and_turns_are_whole(self) -> None:
        await self._pool("forward", response_text=LONG)
        await self._ask("builder|write it all")
        [child] = await self.companion.runtime_store.list_child_requests(
            (await self.companion.runtime_store.list_session_requests("s-1"))[0]["request_id"]
        )
        [trace] = await self.companion.runtime_store.list_recent_traces(
            "builder", session_id=child["session_id"], limit=1
        )
        reader = AgentLogReader(COMPANION_AGENT_ID)
        reader.bind(self.companion.runtime_store, self.companion.memory_store)

        with bound_session_id("s-1"):
            transcript = await InspectAgentTraceTool(reader).execute(
                {"agent_id": "builder", "trace_id": trace["trace_id"], "view": "transcript"}
            )
            turns = await GetAgentTurnsTool(reader).execute(
                {"agent_id": "builder", "trace_id": trace["trace_id"]}
            )
        self.assertFalse(transcript.is_error, transcript.content)
        messages = json.loads(transcript.content)
        self.assertEqual(
            messages,
            [
                {"role": "user", "text": "write it all"},
                {"role": "assistant", "parts": [{"text": LONG}]},
            ],
        )
        [turn] = json.loads(turns.content)
        # The saved reply is the answer's text, stripped.
        self.assertEqual(turn["agent_response"], LONG.strip())

        with bound_session_id("s-other"):
            refused = await InspectAgentTraceTool(reader).execute(
                {"agent_id": "builder", "trace_id": trace["trace_id"], "view": "transcript"}
            )
        self.assertTrue(refused.is_error)


if __name__ == "__main__":
    unittest.main()

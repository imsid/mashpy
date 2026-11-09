"""The companion's agent log tools: ListAgentTraces, InspectAgentTrace, GetAgentTurns."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from mash.agents.companion import COMPANION_AGENT_ID, CompanionSpec
from mash.agents.companion.logs import (
    AgentLogReader,
    GetAgentTurnsTool,
    InspectAgentTraceTool,
    ListAgentTracesTool,
)
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMResponse, LLMTokenUsage
from mash.logging import bound_session_id
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import build_delegating_spec, build_spec, metadata


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
    """Sends 'agent|prompt' to that agent, then answers."""

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request):
        if any(b.type == "tool_result" for m in request.messages for b in m.content):
            return _response("done")
        last = request.messages[-1]
        text = "".join(b.data.get("text", "") for b in last.content if b.type == "text")
        agent_id, prompt = text.split("|", 1)
        call = ToolCall(
            id="call-1", name="RunSubAgents",
            arguments={"requests": [{"agent_id": agent_id, "prompt": prompt}], "timeout_seconds": 10},
        )
        return _response(calls=[call])

    def set_event_logger(self, logger, app_id: str) -> None:
        del logger, app_id


class _Recording:
    """Records every store call made through it."""

    def __init__(self, store: Any, calls: list) -> None:
        self._store = store
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._store, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._calls.append((name, args, kwargs))
            return await target(*args, **kwargs)

        return call


class CompanionLogToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": self._tmp.name, "MASH_DATABASE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        llm = patch.object(CompanionSpec, "build_llm", lambda self: _Router())
        llm.start()
        self.addCleanup(llm.stop)
        self.pool = (
            HostBuilder()
            .agent(build_spec(agent_id="builder", response_text="built it", fail_on_message="explode"), metadata=metadata())
            .agent(
                build_delegating_spec(
                    agent_id="pilot", final_text="p", subagent_id="builder", subagent_prompt="pilot asks"
                ),
                metadata=metadata(),
            )
            .enable_companion()
            .host(Host(host_id="guide", subagents=("builder",)))
            .host(Host(host_id="baseline", primary="pilot", subagents=("builder",)))
            .build()
        )
        self.pool.configure_runtime_database_url("postgresql://test/runtime")
        await self.pool.start()
        self.addAsyncCleanup(self.pool.close)

        self.ok = await self._child("guide", COMPANION_AGENT_ID, "builder|scaffold rss", "s-1")
        self.failed = await self._child("guide", COMPANION_AGENT_ID, "builder|explode now", "s-1")
        self.foreign = await self._child("baseline", "pilot", "go", "s-9")

        runtime = self.pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
        self.calls: list = []
        self.reader = AgentLogReader(COMPANION_AGENT_ID)
        self.reader.bind(
            _Recording(runtime.runtime_store, self.calls), _Recording(runtime.memory_store, self.calls)
        )

    async def _child(self, host_id: str, primary: str, message: str, session_id: str) -> dict:
        accepted = await self.pool.submit_host_request(
            host_id, message=message, session_id=session_id, user_id="u-1"
        )
        async for event in self.pool.get_client(primary).stream_response(accepted["request_id"], timeout=10):
            if event.get("event") in {"request.completed", "request.error"}:
                break
        runtime = self.pool._agent_provider.agent_servers[primary].runtime
        [child] = await runtime.runtime_store.list_child_requests(accepted["request_id"])
        [trace] = await runtime.runtime_store.list_recent_traces("builder", session_id=child["session_id"], limit=1)
        return {**child, "trace_id": trace["trace_id"]}

    async def _call(self, tool_type, args: dict, session_id: str = "s-1"):
        with bound_session_id(session_id):
            result = await tool_type(self.reader).execute(args)
        return result, (json.loads(result.content) if not result.is_error else None)

    async def test_inspect_returns_the_summary_when_view_is_omitted(self) -> None:
        result, summary = await self._call(
            InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"]}
        )
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(summary["request_id"], self.ok["request_id"])
        self.assertEqual(summary["status"], "completed")
        self.assertIn("tool_stats", summary)
        self.assertIn("timing", summary)

    async def test_inspect_events_are_newest_first_and_filtered(self) -> None:
        _, events = await self._call(
            InspectAgentTraceTool,
            {"agent_id": "builder", "trace_id": self.ok["trace_id"], "view": "events",
             "event_type_prefix": "chat", "limit": 3},
        )
        self.assertTrue(events)
        self.assertLessEqual(len(events), 3)
        self.assertTrue(all(e["event_type"].startswith("chat") for e in events))
        ids = [e["event_id"] for e in events]
        self.assertEqual(ids, sorted(ids, reverse=True))

    async def test_list_traces_in_a_child_session(self) -> None:
        _, traces = await self._call(
            ListAgentTracesTool, {"agent_id": "builder", "session_id": self.ok["session_id"]}
        )
        [trace] = traces
        self.assertEqual(trace["trace_id"], self.ok["trace_id"])
        self.assertEqual(trace["request_id"], self.ok["request_id"])
        self.assertEqual(trace["request"], "scaffold rss")

    async def test_get_turns_by_trace_and_by_session(self) -> None:
        _, by_trace = await self._call(
            GetAgentTurnsTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"]}
        )
        self.assertEqual(
            [(t["user_message"], t["agent_response"]) for t in by_trace], [("scaffold rss", "built it")]
        )
        _, by_session = await self._call(
            GetAgentTurnsTool, {"agent_id": "builder", "session_id": self.ok["session_id"]}
        )
        self.assertEqual([t["trace_id"] for t in by_session], [self.ok["trace_id"]])

    async def test_get_turns_needs_a_session_or_trace_and_reports_a_request_with_no_turn(self) -> None:
        result, _ = await self._call(GetAgentTurnsTool, {"agent_id": "builder"})
        self.assertTrue(result.is_error)
        result, _ = await self._call(
            GetAgentTurnsTool, {"agent_id": "builder", "trace_id": self.failed["trace_id"]}
        )
        self.assertTrue(result.is_error)
        self.assertIn("InspectAgentTrace", result.content)
        _, summary = await self._call(
            InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.failed["trace_id"]}
        )
        self.assertEqual(summary["status"], "error")

    async def test_refuses_another_session_another_parent_and_another_agent(self) -> None:
        cases = [
            # The companion's child, asked from another user session.
            (InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"]}, "s-2"),
            (GetAgentTurnsTool, {"agent_id": "builder", "session_id": self.ok["session_id"]}, "s-2"),
            # A child of pilot, not of the companion.
            (InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.foreign["trace_id"]}, "s-1"),
            (ListAgentTracesTool, {"agent_id": "builder", "session_id": self.foreign["session_id"]}, "s-1"),
            # The right trace under the wrong agent.
            (InspectAgentTraceTool, {"agent_id": "pilot", "trace_id": self.ok["trace_id"]}, "s-1"),
        ]
        for tool_type, args, session_id in cases:
            with self.subTest(tool=tool_type.name, args=args, session=session_id):
                self.calls.clear()
                result, _ = await self._call(tool_type, args, session_id)
                self.assertTrue(result.is_error)
                # Refused before any event or turn was read.
                read = {name for name, _, _ in self.calls}
                self.assertFalse(read & {"list_request_events", "list_events", "get_turns", "get_turn"})

    async def test_every_query_is_keyed_by_agent_and_session_or_trace(self) -> None:
        for tool_type, args in (
            (ListAgentTracesTool, {"agent_id": "builder", "session_id": self.ok["session_id"]}),
            (InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"]}),
            (InspectAgentTraceTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"], "view": "events"}),
            (GetAgentTurnsTool, {"agent_id": "builder", "trace_id": self.ok["trace_id"]}),
            (GetAgentTurnsTool, {"agent_id": "builder", "session_id": self.ok["session_id"]}),
        ):
            await self._call(tool_type, args)
        allowed = {
            "list_recent_traces", "get_request_id_for_trace", "list_request_events",
            "list_events", "get_turns", "get_turn", "get_request_identity",
        }
        self.assertTrue(self.calls)
        for name, args, kwargs in self.calls:
            self.assertIn(name, allowed)
            if name == "list_recent_traces":
                self.assertEqual(args[0], "builder")
                self.assertTrue(kwargs["session_id"])
            elif name == "get_request_id_for_trace":
                self.assertEqual(kwargs["app_id"], "builder")
            elif name == "list_events":
                self.assertEqual(args[0], "builder")
                self.assertTrue(kwargs["trace_id"])
            elif name == "get_turns":
                self.assertEqual((kwargs["app_id"], bool(kwargs["session_id"])), ("builder", True))
            elif name == "get_turn":
                self.assertEqual(args[1], "builder")


if __name__ == "__main__":
    unittest.main()

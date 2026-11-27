"""History: how a request sees its session's earlier turns.

- the text of the last ``conversation_history_turns`` turns: each user message
  and reply, with a turn's routes before the next user message
- compaction at a context size summarizes every turn since the last checkpoint
"""

from __future__ import annotations

import asyncio
import copy
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
from mash.memory.routes import build_routes, format_routes
from mash.runtime import AgentRuntime, AgentSpec
from mash.runtime.engine import loop as loop_module
from mash.runtime.engine.rebuild import rebuild_request_state
from mash.runtime.events import RuntimeEventType
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import TEST_IDENTITY
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry

class _Fetch:
    name = "fetch"
    description = "Fetch."
    requires_approval = False
    parallel_safe = True
    parameters = {"type": "object", "properties": {"what": {"type": "string"}}}

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult.success(f"got {args.get('what')}")

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class _Provider(LLMProvider):
    """Per request: fetch what the message names, then answer. Records each call."""

    def __init__(self, usage: int = 10, context: int | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self.usage = usage
        self.context = context
        self.summaries: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        if not request.tools:
            # The compaction summary pass.
            self.summaries.append(request.messages[0].content[0].data["text"])
            return LLMResponse(
                thought_summary="", text="the summary", tool_calls=[],
                content_blocks=[LLMContentBlock.text("the summary")], stop_reason="end_turn",
            )
        self.requests.append(copy.deepcopy(request))
        last = request.messages[-1]
        usage = LLMTokenUsage(
            input_tokens=self.usage,
            output_tokens=1,
            total_tokens=self.usage + 1,
            context_tokens=self.context,
        )
        if last.role == "user":
            what = "".join(b.data.get("text", "") for b in last.content if b.type == "text")
            what = what.rsplit("\n\n", 1)[-1]
            call_id = f"call-{len(self.requests)}"
            return LLMResponse(
                thought_summary="",
                text="",
                tool_calls=[ToolCall(id=call_id, name="fetch", arguments={"what": what})],
                content_blocks=[
                    LLMContentBlock(type="thinking", data={"thinking": "plan", "signature": "sig"}),
                    LLMContentBlock.tool_call(tool_call_id=call_id, name="fetch", arguments={"what": what}),
                ],
                stop_reason="tool_call",
                usage=usage,
            )
        return LLMResponse(
            thought_summary="", text="answered", tool_calls=[],
            content_blocks=[LLMContentBlock.text("answered")], stop_reason="end_turn",
            usage=usage,
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class _Spec(AgentSpec):
    def __init__(self, provider: _Provider, **config: Any) -> None:
        self.provider = provider
        self.config = config

    def get_agent_id(self) -> str:
        return "replay-app"

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(_Fetch())
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(agent_id="replay-app", system_prompt="You are replay-app.", **self.config)


def _shape(request: LLMRequest) -> list[tuple[str, list[str]]]:
    return [(m.role, [b.type for b in m.content]) for m in request.messages]


class ReplayTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def _runtime(self, spec: AgentSpec) -> AgentRuntime:
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="s-1",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.engine.close)
        return runtime

    async def _ask(self, runtime: AgentRuntime, message: str, session_id: str = "s-1") -> Any:
        accepted = await runtime.submit_request(message=message, session_id=session_id, **TEST_IDENTITY)
        for _ in range(500):
            events = await self.runtime_store.list_request_events(accepted["request_id"])
            ends = [e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_END.value]
            if ends:
                self.assertEqual(ends[0].payload["status"], "ok", ends[0].payload)
                return ends[0]
            await asyncio.sleep(0.01)
        self.fail("the request never ended")


class HistoryTests(ReplayTestCase):
    async def test_a_later_request_sees_the_earlier_messages_and_replies(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))
        await self._ask(runtime, "small")
        await self._ask(runtime, "next")

        # The earlier turn's tool call and result are not replayed.
        self.assertEqual(
            [(m.role, m.content[0].data.get("text")) for m in provider.requests[2].messages],
            [("user", "small"), ("assistant", "answered"), ("user", "next")],
        )

    async def test_a_request_replays_only_the_last_turns(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider, conversation_history_turns=2))
        for message in ("one", "two", "three", "four"):
            await self._ask(runtime, message)

        self.assertEqual(
            [m.content[0].data.get("text") for m in provider.requests[6].messages],
            ["two", "answered", "three", "answered", "four"],
        )

    async def test_zero_history_turns_shows_no_history(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider, conversation_history_turns=0))
        await self._ask(runtime, "small")
        await self._ask(runtime, "next")

        self.assertEqual(_shape(provider.requests[2]), [("user", ["text"])])

    async def test_routes_go_before_the_next_message(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))
        await self._ask(runtime, "small")
        [turn] = await self.memory_store.get_turns("s-1", "replay-app")
        routes = [{"agent_id": "web", "session_id": "sess-9", "trace_id": "tr-9", "status": "completed"}]
        turn["metadata"]["routes"] = routes  # the turn as a companion saves it
        for stored in self.memory_store._turns:
            if stored["trace_id"] == turn["trace_id"]:
                stored["metadata"] = turn["metadata"]

        await self._ask(runtime, "next")

        messages = provider.requests[2].messages
        self.assertEqual([m.role for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1].content[0].data["text"], "answered")
        self.assertEqual(
            messages[2].content[0].data["text"], format_routes(routes) + "\n\nnext"
        )


class RoutesTests(unittest.TestCase):
    def test_routes_come_from_full_results(self) -> None:
        import json

        routes = build_routes(
            [
                (
                    "RunSubAgents",
                    {"requests": [{"agent_id": "web", "prompt": "p"}]},
                    json.dumps(
                        [
                            {"agent_id": "web", "session_id": "s-a", "trace_id": "t-a",
                             "status": "completed", "response": "a long answer"},
                            {"agent_id": "feed", "session_id": "s-b", "status": "failed"},
                        ]
                    ),
                ),
                ("RunWorkflow", {"workflow_id": "import"}, json.dumps(
                    {"run_id": "r-1", "trace_id": "t-w", "status": "completed", "result": {}}
                )),
                ("RunSubAgents", {}, "not json"),
            ]
        )
        self.assertEqual(
            routes,
            [
                {"agent_id": "web", "session_id": "s-a", "trace_id": "t-a", "status": "completed"},
                {"agent_id": "feed", "session_id": "s-b", "trace_id": None, "status": "failed"},
                {"workflow_id": "import", "run_id": "r-1", "trace_id": "t-w", "status": "completed"},
            ],
        )
        self.assertEqual(
            format_routes(routes).splitlines()[2:],
            [
                "- web  session s-a  trace t-a  completed",
                "- feed  session s-b  failed",
                "- workflow import  run r-1  trace t-w  completed",
                "</routes>",
            ],
        )
        self.assertEqual(format_routes([]), "")


class CompactionTests(ReplayTestCase):
    async def test_a_session_compacts_once_its_context_reaches_the_threshold(self) -> None:
        provider = _Provider(usage=600)
        runtime = await self._runtime(_Spec(provider, compaction_token_threshold=500))
        first = await self._ask(runtime, "small")
        self.assertEqual(first.payload["context_tokens"], 601)

        second = await self._ask(runtime, "next")

        [summary_input] = provider.summaries
        self.assertEqual(summary_input.count("User: small\nAssistant: answered"), 1)
        self.assertNotIn("fetch", summary_input)
        self.assertEqual(second.payload["compaction_summary_text"], "the summary")
        # The request replays the summary in place of the first turn.
        self.assertEqual(
            [(m.role, m.content[0].data.get("text")) for m in provider.requests[2].messages],
            [("assistant", "the summary"), ("user", "next")],
        )
        turns = await self.memory_store.get_turns("s-1", "replay-app")
        checkpoint = next(t for t in turns if t["metadata"].get("type") == "summary_checkpoint")
        self.assertEqual(checkpoint["metadata"]["turn_ids"], [first.trace_id])
        self.assertEqual(checkpoint["context_tokens"], 0)

    async def test_the_provider_context_size_decides_when_cached_input_is_left_out(self) -> None:
        # Like Anthropic: few uncached input tokens, a large cached context.
        provider = _Provider(usage=10, context=900)
        runtime = await self._runtime(_Spec(provider, compaction_token_threshold=500))
        first = await self._ask(runtime, "small")
        self.assertEqual(first.payload["context_tokens"], 900)
        chat_end = next(
            e
            for e in await self.runtime_store.list_request_events(first.request_id)
            if e.event_type == RuntimeEventType.CHAT_END.value
        )
        self.assertEqual(chat_end.payload["context_tokens"], 900)

        await self._ask(runtime, "next")
        self.assertEqual(len(provider.summaries), 1)

    async def test_compaction_summarizes_turns_older_than_the_history(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider, conversation_history_turns=1))
        first = await self._ask(runtime, "small")
        second = await self._ask(runtime, "next")

        await runtime.compact_session("s-1")

        [summary_input] = provider.summaries
        self.assertIn("User: small", summary_input)
        self.assertIn("User: next", summary_input)
        turns = await self.memory_store.get_turns("s-1", "replay-app")
        checkpoint = next(t for t in turns if t["metadata"].get("type") == "summary_checkpoint")
        self.assertEqual(checkpoint["metadata"]["turn_ids"], [first.trace_id, second.trace_id])

    async def test_a_threshold_of_zero_never_compacts(self) -> None:
        provider = _Provider(usage=600)
        runtime = await self._runtime(_Spec(provider, compaction_token_threshold=0))
        await self._ask(runtime, "small")
        await self._ask(runtime, "next")
        self.assertEqual(provider.summaries, [])


class RebuildWithHistoryTests(ReplayTestCase):
    async def test_a_recovered_request_rebuilds_the_history_it_loaded(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))
        await self._ask(runtime, "small")

        loaded: list[dict[str, Any]] = []
        load = loop_module.load_request_context

        async def _load(*args: Any, **kwargs: Any) -> dict[str, Any]:
            state = await load(*args, **kwargs)
            loaded.append(copy.deepcopy(state))
            return state

        with patch.object(loop_module, "load_request_context", _load):
            second = await self._ask(runtime, "next")
        # A turn saved after the load does not change what a recovery replays.
        await self.memory_store.append_entry(
            trace_id="later", session_id="s-1", agent_id="replay-app",
            user_message="later", agent_response="later", signals={}, context_tokens=0,
        )

        events = await self.runtime_store.list_request_events(second.request_id)
        load_end = next(
            e for e in events if e.event_type == RuntimeEventType.CONTEXT_LOAD_END.value
        )
        rebuilt = await rebuild_request_state(
            runtime,
            session_id="s-1",
            message="next",
            request_metadata=dict(
                next(
                    e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value
                ).payload["request_metadata"]
            ),
            events=[e for e in events if e.event_id <= load_end.event_id],
        )
        self.assertEqual(rebuilt, loaded[0])
        self.assertEqual(len(load_end.payload["history"]), 1)


if __name__ == "__main__":
    unittest.main()

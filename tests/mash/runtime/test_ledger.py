"""The ledger: RunSubAgents results kept in the caller's turns and replayed."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.llm import LLMProvider
from mash.core.context import ToolCall
from mash.core.llm.types import LLMContentBlock, LLMResponse, LLMTokenUsage
from mash.memory.compaction import compact_conversation
from mash.memory.ledger import build_ledger, format_ledger, merge_ledgers
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import DeterministicAgentSpec, metadata, patch_companion_llm


def _context(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": list(messages)}


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}],
    }


def _result(call_id: str, content: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": [
            {
                "type": "tool_result",
                "tool_call_id": call_id,
                "content": content if isinstance(content, str) else json.dumps(content),
                "is_error": is_error,
            }
        ],
    }


class BuildLedgerTests(unittest.TestCase):
    def test_one_entry_per_item_with_both_session_ids_and_the_trace(self) -> None:
        context = _context(
            {"role": "user", "content": "do two things"},
            _call(
                "c1",
                "RunSubAgents",
                {
                    "requests": [
                        {"agent_id": "builder", "prompt": "scaffold rss " + "x" * 300},
                        {"agent_id": "pilot", "prompt": "explain ids"},
                    ]
                },
            ),
            _result(
                "c1",
                [
                    {
                        "agent_id": "builder",
                        "request_id": "r1",
                        "session_id": "sess-rss",
                        "trace_id": "t1",
                        "status": "completed",
                        "response": "y" * 900,
                    },
                    {
                        "agent_id": "pilot",
                        "request_id": "r2",
                        "session_id": "sess-ids",
                        "status": "failed",
                        "error": "boom",
                    },
                ],
            ),
            _call("c2", "search_cortex", {"q": "x"}),
            _result("c2", "irrelevant"),
        )

        ledger = build_ledger(context, "user-session")

        self.assertEqual(len(ledger), 2)
        first, second = ledger
        self.assertEqual(first["companion_session_id"], "user-session")
        self.assertEqual(
            (first["agent_id"], first["session_id"], first["trace_id"], first["status"]),
            ("builder", "sess-rss", "t1", "completed"),
        )
        self.assertEqual(len(first["asked"]), 200)
        self.assertEqual(len(first["got"]), 500)
        self.assertEqual((second["status"], second["got"]), ("failed", "boom"))

    def test_an_error_result_and_history_text_add_nothing(self) -> None:
        context = _context(
            {"role": "assistant", "content": "earlier reply", "metadata": {"source": "history"}},
            _call("c1", "RunSubAgents", {"requests": []}),
            _result("c1", "requests must be a non-empty array", is_error=True),
        )
        self.assertEqual(build_ledger(context, "s"), [])

    def test_merge_keeps_the_latest_entry_per_agent_session(self) -> None:
        old = {"agent_id": "builder", "session_id": "a", "got": "old"}
        other = {"agent_id": "builder", "session_id": "b", "got": "b"}
        new = {"agent_id": "builder", "session_id": "a", "got": "new"}
        self.assertEqual(merge_ledgers([[old, other], [new]]), [other, new])
        many = [[{"agent_id": "x", "session_id": str(i)} for i in range(40)]]
        merged = merge_ledgers(many)
        self.assertEqual(len(merged), 30)
        self.assertEqual(merged[-1]["session_id"], "39")

    def test_format_names_each_session_and_trace_in_full(self) -> None:
        text = format_ledger(
            [
                {
                    "agent_id": "builder",
                    "session_id": "0b6e-full-id",
                    "trace_id": "7a1c-full-id",
                    "status": "completed",
                    "asked": "Scaffold",
                    "got": "Created",
                }
            ]
        )
        self.assertTrue(text.startswith("[ledger]\n- builder  session 0b6e-full-id"))
        self.assertIn("trace 7a1c-full-id  completed", text)
        self.assertIn('asked: "Scaffold"', text)
        self.assertEqual(format_ledger([]), "")


class _SummaryLLM(LLMProvider):
    @property
    def model(self) -> str:
        return "summary"

    async def send(self, request):
        return LLMResponse(
            thought_summary="",
            text="the summary",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("the summary")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def set_event_logger(self, logger, app_id: str) -> None:
        del logger, app_id


class CompactionLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_checkpoint_keeps_the_latest_entry_per_agent_session(self) -> None:
        _, store = build_test_stores()
        for index, ledger in enumerate(
            [
                [{"agent_id": "builder", "session_id": "rss", "got": "v1"}],
                [{"agent_id": "builder", "session_id": "csv", "got": "c"}],
                [{"agent_id": "builder", "session_id": "rss", "got": "v2"}],
                [],
            ]
        ):
            await store.save_turn(
                trace_id=f"t{index}", session_id="s", app_id="companion",
                user_message=f"m{index}", agent_response=f"r{index}", signals={},
                session_total_tokens=0, metadata={"ledger": ledger} if ledger else {},
            )

        _, trace_id = await compact_conversation(
            store=store, llm=_SummaryLLM(), app_id="companion", session_id="s",
            max_tokens=100, temperature=0.0, turn_limit=2, reason="manual",
            session_total_tokens_reset=0,
        )

        [checkpoint] = [t for t in await store.get_turns("s", "companion") if t["trace_id"] == trace_id]
        self.assertEqual(
            [(e["session_id"], e["got"]) for e in checkpoint["metadata"]["ledger"]],
            [("csv", "c"), ("rss", "v2")],
        )


class _RoutingProvider(LLMProvider):
    """Delegates the first message; routes a follow-up to the session its ledger names."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request):
        texts = [
            str(block.data.get("text", ""))
            for message in request.messages
            for block in message.content
            if block.type == "text"
        ]
        self.seen.append("\n".join(texts))
        if any(block.type == "tool_result" for m in request.messages for block in m.content):
            return self._text("answered")
        item: dict[str, Any] = {"agent_id": "research", "prompt": texts[-1]}
        match = re.search(r"- research  session (\S+)", "\n".join(texts))
        if match:
            item["session_id"] = match.group(1)
        arguments = {"requests": [item]}
        return LLMResponse(
            thought_summary="",
            text="",
            tool_calls=[ToolCall(id="call-1", name="RunSubAgents", arguments=arguments)],
            content_blocks=[
                LLMContentBlock.tool_call(tool_call_id="call-1", name="RunSubAgents", arguments=arguments)
            ],
            stop_reason="tool_call",
            usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def _text(self, text: str) -> LLMResponse:
        return LLMResponse(
            thought_summary="",
            text=text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def set_event_logger(self, logger, app_id: str) -> None:
        del logger, app_id


class LedgerReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_follow_up_routed_from_the_ledger_lands_in_the_recorded_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = _RoutingProvider()
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}), patch_companion_llm(router):
                pool = (
                    HostBuilder()
                    .agent(DeterministicAgentSpec(agent_id="research", response_text="research-ok"), metadata=metadata())
                    .host(Host(host_id="h", agents=("research",)))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    client = pool.get_client("companion")

                    async def _ask(message: str) -> str:
                        accepted = await pool.submit_host_request(
                            "h", message=message, session_id="s-1", user_id="u-1"
                        )
                        async for event in client.stream_response(accepted["request_id"], timeout=10):
                            if event.get("event") in {"request.completed", "request.error"}:
                                self.assertEqual(event["event"], "request.completed", event)
                                return accepted["request_id"]
                        raise AssertionError("no terminal event")

                    first_id = await _ask("scaffold rss")
                    runtime = pool._agent_provider.agent_servers["companion"].runtime
                    [turn] = await runtime.store.get_turns("s-1", "companion")
                    [entry] = turn["metadata"]["ledger"]
                    self.assertEqual(entry["companion_session_id"], "s-1")
                    self.assertEqual(entry["agent_id"], "research")
                    self.assertTrue(entry["trace_id"])
                    self.assertEqual(entry["got"], "research-ok")
                    # The stored reply stays the reply text.
                    self.assertEqual(turn["agent_response"], "answered")

                    second_id = await _ask("add a test")

                    # The replayed reply showed the ledger to the model.
                    self.assertIn(f"answered\n\n[ledger]\n- research  session {entry['session_id']}", router.seen[2])
                    [first_child] = await runtime.runtime_store.list_child_requests(first_id)
                    [second_child] = await runtime.runtime_store.list_child_requests(second_id)
                    self.assertEqual(first_child["session_id"], entry["session_id"])
                    self.assertEqual(second_child["session_id"], entry["session_id"])
                finally:
                    await pool.close()


if __name__ == "__main__":
    unittest.main()

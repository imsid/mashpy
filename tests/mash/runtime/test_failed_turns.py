"""A failed request's user message is saved as a turn and replayed with its error."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import build_test_stores
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMResponse, LLMTokenUsage
from mash.memory.compaction import compact_conversation
from mash.memory.failed_turns import failed_turn_metadata, format_failed_request
from mash.runtime import Host, HostBuilder
from mash.runtime.engine import loop as loop_module
from mash.runtime.engine.loop import RequestRun
from mash.testing.runtime_fixtures import DeterministicAgentSpec, metadata, patch_companion_llm

_MALFORMED = "Error code: 400 - bad request"


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        thought_summary="",
        text=text,
        tool_calls=[],
        content_blocks=[LLMContentBlock.text(text)],
        stop_reason="end_turn",
        usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _FailsSecondProvider(LLMProvider):
    """Answers the first and third requests; the second one's call fails terminally."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request):
        self.calls.append(
            [
                (
                    message.role,
                    "".join(
                        str(block.data.get("text", ""))
                        for block in message.content
                        if block.type == "text"
                    ),
                )
                for message in request.messages
            ]
        )
        if len(self.calls) == 2:
            raise RuntimeError(_MALFORMED)
        return _text_response(f"answer {len(self.calls)}")

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class FormatFailedRequestTests(unittest.TestCase):
    def test_names_the_error_and_the_message(self) -> None:
        text = format_failed_request(
            "dive into it",
            failed_turn_metadata({"error_code": "invalid_request", "error": "400  bad\nrequest"}),
        )
        self.assertEqual(
            text,
            "<failed_request>\n"
            "This user message got no reply: the request failed, recorded by the system.\n"
            "Error: invalid_request: 400 bad request\n"
            "User message: dive into it\n"
            "</failed_request>",
        )

    def test_clips_a_long_error(self) -> None:
        text = format_failed_request("m", failed_turn_metadata({"error": "x" * 1000}))
        [line] = [line for line in text.splitlines() if line.startswith("Error: ")]
        self.assertEqual(len(line), len("Error: ") + 300)


class FailedTurnReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_try_again_sees_the_failed_message_and_its_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _FailsSecondProvider()
            with patch.dict(
                os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
            ), patch_companion_llm(provider):
                pool = (
                    HostBuilder()
                    .agent(DeterministicAgentSpec(agent_id="research"), metadata=metadata())
                    .host(Host(host_id="h", agents=("research",)))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    client = pool.get_client("companion")

                    async def _ask(message: str) -> str:
                        accepted = await pool.submit_host_request(
                            "h", message=message, session_id="s-1", app_id="app-1", policy_id="p-1"
                        )
                        async for event in client.stream_response(accepted["request_id"], timeout=10):
                            if event.get("event") in {"request.completed", "request.error"}:
                                return str(event["event"])
                        raise AssertionError("no terminal event")

                    self.assertEqual(await _ask("refresh my follows"), "request.completed")
                    self.assertEqual(await _ask("dive into the Nvidia story"), "request.error")

                    runtime = pool._agent_provider.agent_servers["companion"].runtime
                    first, failed = await runtime.store.get_turns("s-1", "companion")
                    self.assertEqual(failed["user_message"], "dive into the Nvidia story")
                    self.assertEqual(failed["agent_response"], "")
                    self.assertEqual(failed["metadata"]["type"], "failed_request")
                    self.assertEqual(failed["metadata"]["error"]["error_code"], "invalid_request")
                    self.assertEqual(
                        failed["session_total_tokens"], first["session_total_tokens"]
                    )

                    self.assertEqual(await _ask("try again"), "request.completed")

                    # The failed request adds no message of its own: the
                    # history stays user / assistant / user, and the failure
                    # sits at the top of the new message.
                    third = provider.calls[2]
                    self.assertEqual(
                        [role for role, _ in third], ["user", "assistant", "user"]
                    )
                    self.assertEqual(third[0][1], "refresh my follows")
                    self.assertEqual(third[1][1], "answer 1")
                    self.assertEqual(
                        third[2][1],
                        "<failed_request>\n"
                        "This user message got no reply: the request failed, "
                        "recorded by the system.\n"
                        f"Error: invalid_request: {_MALFORMED}\n"
                        "User message: dive into the Nvidia story\n"
                        "</failed_request>\n\n"
                        "try again",
                    )

                    # Cortex reads only the turns that got a reply.
                    app_turns = await runtime.store.list_app_turns("app-1")
                    self.assertEqual(
                        [t["user_message"] for t in app_turns],
                        ["refresh my follows", "try again"],
                    )
                    self.assertEqual(await runtime.store.count_app_turns("app-1"), 2)
                finally:
                    await pool.close()


class _SummaryLLM(LLMProvider):
    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "summary"

    async def send(self, request):
        self.seen.append(
            "".join(
                str(block.data.get("text", ""))
                for message in request.messages
                for block in message.content
            )
        )
        return _text_response("the summary")

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class FailedTurnCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_leaves_the_failed_turn_out(self) -> None:
        _, store = build_test_stores()
        await store.save_turn(
            trace_id="t0", session_id="s", agent_id="a", user_message="asked",
            agent_response="answered", signals={}, session_total_tokens=0,
        )
        await store.save_turn(
            trace_id="t1", session_id="s", agent_id="a", user_message="failed ask",
            agent_response="", signals={}, session_total_tokens=0,
            metadata=failed_turn_metadata({"error_code": "timeout", "error": "boom"}),
        )
        llm = _SummaryLLM()

        _, trace_id = await compact_conversation(
            store=store, llm=llm, agent_id="a", session_id="s", max_tokens=100,
            temperature=0.0, turn_limit=5, reason="manual", session_total_tokens_reset=0,
        )

        self.assertEqual(llm.seen, ["User: asked\nAssistant: answered"])
        [checkpoint] = [t for t in await store.get_turns("s", "a") if t["trace_id"] == trace_id]
        self.assertEqual(checkpoint["metadata"]["turn_ids"], ["t0"])


class FailedTurnScopeTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, request_metadata: dict[str, Any]) -> RequestRun:
        run = RequestRun(
            MagicMock(),
            request_id="r1",
            message="hello",
            session_id="s",
            request_metadata=request_metadata,
        )
        run.trace_id = "t1"
        return run

    async def test_a_request_with_a_parent_saves_no_failed_turn(self) -> None:
        for parent in (
            {"kind": "subagent", "request_id": "p"},
            {"kind": "workflow", "workflow_id": "w"},
        ):
            with patch.object(loop_module, "persist_failed_turn", AsyncMock()) as persist:
                await self._run({"parent": parent})._persist_failed_turn({"error": "x"})
            persist.assert_not_awaited()

    async def test_a_failed_save_is_logged_and_does_not_raise(self) -> None:
        with patch.object(
            loop_module, "persist_failed_turn", AsyncMock(side_effect=RuntimeError("db down"))
        ) as persist, self.assertLogs("mash.runtime.engine.loop", level="ERROR"):
            await self._run({})._persist_failed_turn({"error": "x"})
        persist.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

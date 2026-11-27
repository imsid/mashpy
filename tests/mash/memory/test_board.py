"""Board compaction: one summary per board, at the companion's threshold."""

from __future__ import annotations

import unittest
from typing import Any

from conftest import build_test_stores
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.memory.board import compact_board, render_board

SESSION = "conv-1"


class _Summarizer(LLMProvider):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    @property
    def model(self) -> str:
        return "summarizer"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.inputs.append(request.messages[0].content[0].data["text"])
        return LLMResponse(
            thought_summary="",
            text="the summary",
            tool_calls=[],
            content_blocks=[LLMContentBlock.text("the summary")],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=500, output_tokens=12, total_tokens=512),
        )

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


class BoardCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _, self.store = build_test_stores()
        self.llm = _Summarizer()

    async def _entry(self, name: str, agent_id: str, tokens: int, **metadata: Any) -> None:
        await self.store.append_entry(
            trace_id=name,
            session_id=SESSION,
            agent_id=agent_id,
            user_message=f"asked {name}",
            agent_response=f"{agent_id} answered {name}",
            signals={},
            context_tokens=tokens,
            tokens=tokens,
            metadata=metadata or None,
        )

    async def _compact(self, trace_id: str = "sum", **kwargs: Any) -> Any:
        return await compact_board(
            self.store,
            self.llm,
            session_id=SESSION,
            agent_id="companion",
            trace_id=trace_id,
            threshold=kwargs.pop("threshold", 100),
            max_tokens=1000,
            temperature=0.0,
            **kwargs,
        )

    async def test_below_the_threshold_nothing_is_written(self) -> None:
        await self._entry("e1", "inbox", 60)
        await self._entry("e2", "fudge", 39)

        self.assertIsNone(await self._compact())
        self.assertEqual(self.llm.inputs, [])

    async def test_at_the_threshold_one_summary_covers_the_board(self) -> None:
        await self._entry("e1", "inbox", 60)
        await self._entry("e2", "fudge", 40)

        summary = await self._compact()

        self.assertEqual(summary["seq"], 3)
        self.assertEqual(summary["agent_id"], "companion")
        self.assertEqual(summary["agent_response"], "the summary")
        self.assertEqual(summary["metadata"]["covers"], [1, 2])
        # The summary's size is the summary call's output; the count starts again.
        self.assertEqual((summary["tokens"], summary["board_tokens"]), (12, 12))
        [text] = self.llm.inputs
        self.assertIn("[entry 1 · inbox]\nUser: asked e1\ninbox: inbox answered e1", text)
        self.assertIn("[entry 2 · fudge]", text)
        # Running it again (a recovered request) writes nothing more.
        self.assertIsNone(await self._compact())
        self.assertEqual(len(await self.store.read_entries(SESSION)), 3)

    async def test_the_next_summary_covers_the_previous_one_and_after(self) -> None:
        await self._entry("e1", "inbox", 100)
        await self._compact("sum-1")
        await self._entry("e3", "fudge", 95)

        summary = await self._compact("sum-2")

        self.assertEqual(summary["metadata"]["covers"], [2, 3])
        self.assertIn("summary of the entries before it", self.llm.inputs[-1])
        self.assertNotIn("asked e1", self.llm.inputs[-1])

    async def test_manual_compaction_ignores_the_threshold(self) -> None:
        await self._entry("e1", "inbox", 1)

        summary = await self._compact(reason="manual")

        self.assertEqual(summary["metadata"]["reason"], "manual")
        self.assertIsNone(await self._compact("again", reason="manual"))

    async def test_a_threshold_of_zero_never_compacts(self) -> None:
        await self._entry("e1", "inbox", 10_000)
        self.assertIsNone(await self._compact(threshold=0))

    def test_render_board_labels_failed_and_workflow_entries(self) -> None:
        text = render_board(
            [
                {
                    "seq": 1,
                    "agent_id": "inbox",
                    "user_message": "hi",
                    "agent_response": "",
                    "metadata": {"type": "failed_request", "error": {"error": "boom"}},
                },
                {
                    "seq": 2,
                    "agent_id": "companion",
                    "user_message": "import",
                    "agent_response": "ran",
                    "metadata": {"type": "workflow_result"},
                },
            ]
        )
        self.assertIn("[entry 1 · inbox · failed]\n<failed_request>", text)
        self.assertIn("[entry 2 · companion · workflow run]\nUser: import\ncompanion: ran", text)

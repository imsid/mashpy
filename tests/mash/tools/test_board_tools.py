"""read_board and search_board read the request's own session, by number or words."""

from __future__ import annotations

import json
import unittest

from conftest import build_test_stores
from mash.tools.board import ReadBoardTool, SearchBoardTool, board_tools


class BoardToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _, self.store = build_test_stores()
        for index, (session_id, agent_id, text) in enumerate(
            [
                ("conv", "inbox", "the lisbon itinerary"),
                ("conv", "fudge", "a timeline"),
                ("other", "inbox", "lisbon elsewhere"),
                ("conv", "companion", "summary"),
                ("conv", "fudge", "lisbon timeline"),
            ]
        ):
            await self.store.append_entry(
                trace_id=f"t{index}",
                session_id=session_id,
                agent_id=agent_id,
                user_message=f"ask {index}",
                agent_response=text,
                signals={},
                context_tokens=0,
                metadata={"type": "summary"} if agent_id == "companion" else None,
            )

    async def _read(self, **args) -> list[dict]:
        result = await ReadBoardTool(self.store, "conv").execute(args)
        self.assertFalse(result.is_error, result.content)
        return json.loads(result.content)

    async def test_read_board_by_number(self) -> None:
        self.assertEqual([e["seq"] for e in await self._read()], [1, 2, 3, 4])
        self.assertEqual([e["seq"] for e in await self._read(after=2)], [3, 4])
        self.assertEqual([e["seq"] for e in await self._read(before=4, limit=2)], [2, 3])
        [entry] = await self._read(after=2, limit=1)
        self.assertEqual(
            entry,
            {
                "seq": 3,
                "agent_id": "companion",
                "type": "summary",
                "user_message": "ask 3",
                "agent_response": "summary",
            },
        )
        error = await ReadBoardTool(self.store, "conv").execute({"after": "x"})
        self.assertTrue(error.is_error)

    async def test_search_board_stays_in_the_session(self) -> None:
        result = await SearchBoardTool(self.store, "conv").execute({"query": "lisbon"})
        hits = json.loads(result.content)
        self.assertEqual(sorted(h["seq"] for h in hits), [1, 4])
        self.assertEqual({h["agent_id"] for h in hits}, {"inbox", "fudge"})
        self.assertTrue((await SearchBoardTool(self.store, "conv").execute({})).is_error)

    def test_neither_tool_takes_a_session(self) -> None:
        for tool in board_tools(self.store, "conv"):
            properties = tool.to_llm_format()["input_schema"]["properties"]
            self.assertNotIn("session_id", properties)

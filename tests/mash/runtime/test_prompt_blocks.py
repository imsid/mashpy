"""Unit tests for system-prompt block helpers."""

from __future__ import annotations

import unittest

from mash.runtime.host.subagents import append_context_block


class AppendContextBlockTests(unittest.TestCase):
    def test_none_leaves_string_prompt_unchanged(self) -> None:
        self.assertEqual(append_context_block("base", None), "base")

    def test_blank_leaves_prompt_unchanged(self) -> None:
        self.assertEqual(append_context_block("base", "   \n"), "base")

    def test_appends_to_string_prompt(self) -> None:
        self.assertEqual(
            append_context_block("base", "extra context"),
            "base\n\nextra context",
        )

    def test_appends_block_to_list_prompt(self) -> None:
        base = [{"type": "text", "text": "base"}]
        result = append_context_block(base, "extra context")
        self.assertEqual(
            result,
            [
                {"type": "text", "text": "base"},
                {"type": "text", "text": "extra context"},
            ],
        )
        # Original prompt is not mutated.
        self.assertEqual(base, [{"type": "text", "text": "base"}])

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(
            append_context_block("base", "  padded  "),
            "base\n\npadded",
        )


if __name__ == "__main__":
    unittest.main()

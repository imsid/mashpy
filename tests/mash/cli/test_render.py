"""Tests for CLI rendering."""

from __future__ import annotations

import unittest

from rich.console import Console

from mash.cli.render import RichRenderer


class RichRendererTests(unittest.TestCase):
    def test_markdown_panel_wraps_within_console_width(self) -> None:
        console = Console(width=80, record=True)
        renderer = RichRenderer(console=console)

        renderer.markdown(
            "I'm an AI assistant here to help with answering questions, "
            "brainstorming, writing, or reviewing long text without clipping "
            "the panel borders in a narrow terminal."
        )

        output = console.export_text(styles=False)
        lines = [line for line in output.splitlines() if line]
        self.assertTrue(lines)
        self.assertTrue(any("Assistant" in line for line in lines))
        self.assertTrue(all(len(line) <= 80 for line in lines))


if __name__ == "__main__":
    unittest.main()

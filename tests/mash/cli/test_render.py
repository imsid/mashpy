"""Tests for CLI rendering."""

from __future__ import annotations

import unittest

from rich.console import Console

from mash.cli.chain_renderer import ChainOfThoughtRenderer
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


class ChainRendererTokenDisplayTests(unittest.TestCase):
    """Tests for reasoning token display in per-step output."""

    def _renderer(self) -> tuple[ChainOfThoughtRenderer, Console]:
        console = Console(width=120, record=True)
        renderer = ChainOfThoughtRenderer(console=console)
        return renderer, console

    def test_reasoning_tokens_shown_in_step_output_when_present(self) -> None:
        renderer, console = self._renderer()
        step = {
            "action_type": "tool_call",
            "tool_calls": ["search"],
            "tool_calls_detail": [],
            "token_usage": {"input": 150, "output": 450, "reasoning": 380},
            "think_duration": 1200,
            "display_step": 1,
        }

        renderer._render_think(step)

        output = console.export_text(styles=False)
        self.assertIn("150+450 tokens", output)
        self.assertIn("380 reasoning", output)

    def test_reasoning_tokens_omitted_when_absent(self) -> None:
        renderer, console = self._renderer()
        step = {
            "action_type": "response",
            "tool_calls": [],
            "tool_calls_detail": [],
            "token_usage": {"input": 100, "output": 50},
            "think_duration": 800,
            "display_step": 1,
        }

        renderer._render_think(step)

        output = console.export_text(styles=False)
        self.assertIn("100+50 tokens", output)
        self.assertNotIn("reasoning", output)

    def test_subagent_step_shows_reasoning_tokens(self) -> None:
        renderer, console = self._renderer()
        from mash.runtime.events import RuntimeEvent, RuntimeEventType

        event = RuntimeEvent(
            app_id="test",
            agent_id="research",
            event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
            payload={
                "action_type": "tool_call",
                "tool_calls_detail": [],
                "token_usage": {"input": 200, "output": 300, "reasoning": 260},
                "duration_ms": 900,
            },
        )

        renderer.render_subagent_event(event, agent_id="research")

        output = console.export_text(styles=False)
        self.assertIn("200+300 tokens", output)
        self.assertIn("260 reasoning", output)

    def test_subagent_step_omits_reasoning_when_absent(self) -> None:
        renderer, console = self._renderer()
        from mash.runtime.events import RuntimeEvent, RuntimeEventType

        event = RuntimeEvent(
            app_id="test",
            agent_id="assistant",
            event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
            payload={
                "action_type": "response",
                "tool_calls_detail": [],
                "token_usage": {"input": 50, "output": 20},
                "duration_ms": 400,
            },
        )

        renderer.render_subagent_event(event, agent_id="assistant")

        output = console.export_text(styles=False)
        self.assertIn("50+20 tokens", output)
        self.assertNotIn("reasoning", output)


if __name__ == "__main__":
    unittest.main()

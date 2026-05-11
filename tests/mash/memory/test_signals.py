"""Tests for built-in memory signal collectors."""

from __future__ import annotations

import unittest

from mash.memory.signals import build_default_signal_collector


class DefaultSignalCollectorTests(unittest.TestCase):
    def test_default_collector_exposes_built_in_signal_definitions(self) -> None:
        collector = build_default_signal_collector()

        self.assertEqual(
            collector.get_signal_definitions(),
            {
                "unused_tools": {
                    "name": "unused_tools",
                    "value_type": "string_list",
                    "description": (
                        "Tools offered to the model but never invoked during the "
                        "completed trace."
                    ),
                    "computed_at": "turn_complete",
                    "persisted": True,
                },
                "unused_tool_tokens": {
                    "name": "unused_tool_tokens",
                    "value_type": "integer",
                    "description": (
                        "Estimated token footprint of tool definitions offered but "
                        "never invoked during the completed trace."
                    ),
                    "computed_at": "turn_complete",
                    "persisted": True,
                },
            },
        )

    def test_all_tools_are_unused_when_trace_never_calls_any_tool(self) -> None:
        collector = build_default_signal_collector()
        signals = collector.collect(
            {
                "context": None,
                "action": None,
                "results": [],
                "tool_usage": {
                    "alpha": {"tokens": 10, "invocations": 0},
                    "beta": {"tokens": 25, "invocations": 0},
                },
            }
        )

        self.assertEqual(signals["unused_tools"], ["alpha", "beta"])
        self.assertEqual(signals["unused_tool_tokens"], 35)

    def test_used_tools_are_removed_even_if_called_multiple_times(self) -> None:
        collector = build_default_signal_collector()

        signals = collector.collect(
            {
                "context": None,
                "action": None,
                "results": [],
                "tool_usage": {
                    "alpha": {"tokens": 10, "invocations": 2},
                    "beta": {"tokens": 20, "invocations": 1},
                    "gamma": {"tokens": 5, "invocations": 0},
                },
            }
        )

        self.assertEqual(signals["unused_tools"], ["gamma"])
        self.assertEqual(signals["unused_tool_tokens"], 5)

    def test_empty_tool_usage_returns_empty_unused_metrics(self) -> None:
        collector = build_default_signal_collector()
        signals = collector.collect(
            {
                "context": None,
                "action": None,
                "results": [],
                "tool_usage": {},
            }
        )

        self.assertEqual(signals["unused_tools"], [])
        self.assertEqual(signals["unused_tool_tokens"], 0)


if __name__ == "__main__":
    unittest.main()

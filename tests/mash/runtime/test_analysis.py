"""Trace analysis over the OpenTelemetry projection."""

from __future__ import annotations

import unittest

from otel_fixtures import RequestEvents, completed_request

from mash.otel.projection import project_request
from mash.runtime.events import analyze_projection


def _analysis(events, **kw):
    return analyze_projection(project_request(events, capture_content=False, **kw))


class AnalyzeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analysis = _analysis(completed_request().events)

    def test_timing_breakdown(self) -> None:
        a = self.analysis
        self.assertAlmostEqual(a.total_duration_ms, 4800.0)
        self.assertAlmostEqual(a.cold_start_ms, 500.0)
        self.assertAlmostEqual(a.context_load_ms, 500.0)
        self.assertAlmostEqual(a.total_think_ms, 2000.0)
        self.assertAlmostEqual(a.total_tool_ms, 1500.0)
        self.assertAlmostEqual(a.idle_ms, 300.0)
        self.assertEqual(a.status, "completed")

    def test_tokens_come_from_chat_spans(self) -> None:
        a = self.analysis
        self.assertEqual((a.input_tokens, a.output_tokens), (300, 50))
        self.assertEqual((a.cache_read_tokens, a.cache_write_tokens), (50, 10))

    def test_counts_and_tool_stats(self) -> None:
        a = self.analysis
        self.assertEqual((a.step_count, a.tool_call_count, a.tool_error_count), (2, 1, 0))
        [stats] = a.tool_stats
        self.assertEqual((stats.tool_name, stats.count), ("search", 1))
        self.assertAlmostEqual(stats.total_ms, 1500.0)

    def test_step_breakdown(self) -> None:
        first, second = self.analysis.step_breakdown
        self.assertEqual(first.step_index, 0)
        self.assertEqual(first.tool_calls, ["search"])
        self.assertAlmostEqual(first.think_ms, 1000.0)
        self.assertAlmostEqual(first.tool_ms, 1500.0)
        self.assertEqual(first.token_usage["input"], 100)
        self.assertEqual(second.tool_calls, [])

    def test_slowest_spans_use_span_names(self) -> None:
        slowest = self.analysis.slowest_spans
        self.assertEqual(slowest[0]["name"], "execute_tool search")
        self.assertEqual(slowest[0]["kind"], "execute_tool")
        self.assertEqual(slowest[0]["step_index"], 0)

    def test_digest_shape(self) -> None:
        digest = self.analysis.to_digest_dict()
        self.assertEqual(
            set(digest),
            {
                "timing", "tool_stats", "step_breakdown", "slowest_operations",
                "subagent_traces", "step_count", "tool_call_count", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_write_tokens",
            },
        )

    def test_tool_errors_and_failed_status(self) -> None:
        events = (
            RequestEvents()
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "lookup"}])
            .tool(0, "c1", "lookup", 100.3, 100.4, error=True)
            .step_end(0, 100.5)
            .end(100.6, "error")
            .events
        )
        a = _analysis(events)
        self.assertEqual(a.tool_error_count, 1)
        self.assertEqual(a.status, "error")

    def test_running_request_is_in_progress(self) -> None:
        events = RequestEvents().accepted(100.0).chat_start(0, 100.5).events
        self.assertEqual(_analysis(events).status, "in_progress")

    def test_subagents_are_analysed_from_their_own_spans(self) -> None:
        parent = (
            RequestEvents("parent")
            .accepted(100.0)
            .chat(0, 100.1, 100.2, tool_calls=[{"id": "c1", "name": "RunSubAgents"}])
            .tool(0, "c1", "RunSubAgents", 100.3, 102.0,
                  subagents=[{"agent_id": "research", "request_id": "child"}])
            .step_end(0, 102.1)
            .end(102.2)
        )
        child = (
            RequestEvents("child", agent_id="research", parent_request_id="parent")
            .accepted(100.4)
            .chat(0, 100.5, 101.5, tokens=(7, 3))
            .step_end(0, 101.6)
            .end(101.7)
        )
        a = _analysis(parent.events, children={"child": child.events})
        self.assertAlmostEqual(a.total_subagent_ms, 1700.0)
        self.assertEqual(a.total_tool_ms, 0.0)
        # The parent's tokens do not include the child's.
        self.assertEqual(a.input_tokens, 10)
        [detail] = a.subagent_details
        self.assertEqual((detail.agent_id, detail.request_id), ("research", "child"))
        self.assertIsNotNone(detail.child_analysis)
        self.assertEqual(detail.child_analysis.input_tokens, 7)


if __name__ == "__main__":
    unittest.main()

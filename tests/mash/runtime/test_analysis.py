"""Tests for trace analysis from span trees."""

from __future__ import annotations

import unittest

from mash.runtime.events import build_span_tree, analyze_trace, SpanKind
from mash.runtime.events.types import RuntimeEvent, RuntimeEventType


def _event(
    event_id: int,
    event_type: str,
    created_at: float,
    *,
    app_id: str = "agent-1",
    session_id: str = "s-1",
    trace_id: str = "t-1",
    loop_index: int | None = None,
    payload: dict | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=event_id,
        app_id=app_id,
        agent_id=app_id,
        session_id=session_id,
        trace_id=trace_id,
        event_type=event_type,
        loop_index=loop_index,
        payload=dict(payload or {}),
        created_at=created_at,
    )


def _build_standard_trace_events() -> list[RuntimeEvent]:
    return [
        _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
        _event(2, RuntimeEventType.TRACE_STARTED.value, 1.05),
        _event(3, RuntimeEventType.CONTEXT_LOADED.value, 1.15),
        # Step 0: think + 2 tool calls
        _event(4, RuntimeEventType.LLM_THINK_STARTED.value, 1.15, loop_index=0),
        _event(5, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.55, loop_index=0, payload={
            "duration_ms": 400, "action_type": "tool_call",
            "token_usage": {"input": 100, "output": 50},
        }),
        _event(6, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.75, loop_index=0, payload={
            "duration_ms": 200, "tool_name": "bash",
        }),
        _event(7, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.95, loop_index=0, payload={
            "duration_ms": 150, "tool_name": "bash",
        }),
        _event(8, RuntimeEventType.STEP_COMPLETED.value, 1.95, loop_index=0, payload={"duration_ms": 800}),
        # Step 1: think only (response)
        _event(9, RuntimeEventType.LLM_THINK_STARTED.value, 1.95, loop_index=1),
        _event(10, RuntimeEventType.LLM_THINK_COMPLETED.value, 2.35, loop_index=1, payload={
            "duration_ms": 300, "action_type": "response",
            "token_usage": {"input": 200, "output": 100},
        }),
        _event(11, RuntimeEventType.STEP_COMPLETED.value, 2.35, loop_index=1, payload={"duration_ms": 400}),
        _event(12, RuntimeEventType.REQUEST_COMPLETED.value, 2.40),
    ]


class TraceAnalysisTests(unittest.TestCase):
    def test_basic_timing_breakdown(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(analysis.trace_id, "t-1")
        self.assertEqual(analysis.target_agent_id, "agent-1")
        self.assertEqual(analysis.status, "completed")
        self.assertAlmostEqual(analysis.total_duration_ms, 1400.0, places=0)
        self.assertAlmostEqual(analysis.cold_start_ms, 50.0, places=0)
        self.assertAlmostEqual(analysis.context_load_ms, 100.0, places=0)
        self.assertEqual(analysis.total_think_ms, 700.0)  # 400 + 300
        self.assertEqual(analysis.total_tool_ms, 350.0)   # 200 + 150
        self.assertEqual(analysis.total_subagent_ms, 0.0)
        self.assertGreaterEqual(analysis.idle_ms, 0.0)

    def test_token_aggregation(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(analysis.input_tokens, 300)   # 100 + 200
        self.assertEqual(analysis.output_tokens, 150)  # 50 + 100

    def test_step_count_and_tool_count(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(analysis.step_count, 2)
        self.assertEqual(analysis.tool_call_count, 2)
        self.assertEqual(analysis.tool_error_count, 0)

    def test_tool_stats_grouped_by_name(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(len(analysis.tool_stats), 1)
        bash_stats = analysis.tool_stats[0]
        self.assertEqual(bash_stats.tool_name, "bash")
        self.assertEqual(bash_stats.count, 2)
        self.assertEqual(bash_stats.total_ms, 350.0)
        self.assertEqual(bash_stats.max_ms, 200.0)
        self.assertEqual(bash_stats.min_ms, 150.0)
        self.assertAlmostEqual(bash_stats.avg_ms, 175.0)

    def test_step_breakdown(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(len(analysis.step_breakdown), 2)

        step0 = analysis.step_breakdown[0]
        self.assertEqual(step0.step_index, 0)
        self.assertEqual(step0.think_ms, 400.0)
        self.assertEqual(step0.tool_ms, 350.0)
        self.assertEqual(step0.subagent_ms, 0.0)
        self.assertEqual(step0.total_ms, 800.0)
        self.assertEqual(step0.overhead_ms, 50.0)
        self.assertEqual(step0.tool_calls, ["bash", "bash"])

        step1 = analysis.step_breakdown[1]
        self.assertEqual(step1.step_index, 1)
        self.assertEqual(step1.think_ms, 300.0)
        self.assertEqual(step1.tool_ms, 0.0)
        self.assertEqual(step1.total_ms, 400.0)

    def test_slowest_spans_ranked(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertGreater(len(analysis.slowest_spans), 0)
        self.assertEqual(analysis.slowest_spans[0]["kind"], "think")
        self.assertEqual(analysis.slowest_spans[0]["duration_ms"], 400.0)
        durations = [s["duration_ms"] for s in analysis.slowest_spans]
        self.assertEqual(durations, sorted(durations, reverse=True))

    def test_subagent_details_extracted(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 400, "action_type": "tool_call",
            }),
            _event(3, RuntimeEventType.SUBAGENT_CALL_COMPLETED.value, 3.0, loop_index=0, payload={
                "duration_ms": 1200, "tool_name": "InvokeSubagent",
                "result": {"metadata": {"agent_id": "research", "subagent_session_id": "sub:research:abc", "request_id": "req-1"}},
            }),
            _event(4, RuntimeEventType.STEP_COMPLETED.value, 3.0, loop_index=0, payload={"duration_ms": 2000}),
            _event(5, RuntimeEventType.REQUEST_COMPLETED.value, 3.1),
        ]
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(len(analysis.subagent_details), 1)
        detail = analysis.subagent_details[0]
        self.assertEqual(detail.agent_id, "research")
        self.assertEqual(detail.subagent_session_id, "sub:research:abc")
        self.assertEqual(detail.request_id, "req-1")
        self.assertEqual(detail.duration_ms, 1200.0)
        self.assertIsNone(detail.child_analysis)
        self.assertEqual(analysis.total_subagent_ms, 1200.0)

    def test_timing_percentages(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        timing = analysis.to_timing_dict()
        self.assertIn("pct_think", timing)
        self.assertIn("pct_tool", timing)
        self.assertIn("pct_subagent", timing)
        self.assertIn("pct_cold_start", timing)
        self.assertGreater(timing["pct_think"], 0)
        self.assertGreater(timing["pct_tool"], 0)
        self.assertEqual(timing["pct_subagent"], 0.0)

    def test_tool_error_counting(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 400, "action_type": "tool_call",
            }),
            _event(3, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.8, loop_index=0, payload={
                "duration_ms": 200, "tool_name": "bash", "is_error": True,
            }),
            _event(4, RuntimeEventType.TOOL_CALL_COMPLETED.value, 2.0, loop_index=0, payload={
                "duration_ms": 150, "tool_name": "bash",
            }),
            _event(5, RuntimeEventType.STEP_COMPLETED.value, 2.0, loop_index=0, payload={"duration_ms": 1000}),
            _event(6, RuntimeEventType.REQUEST_COMPLETED.value, 2.1),
        ]
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(analysis.tool_call_count, 2)
        self.assertEqual(analysis.tool_error_count, 1)
        self.assertEqual(analysis.tool_stats[0].error_count, 1)

    def test_empty_trace_analysis(self) -> None:
        tree = build_span_tree([])
        analysis = analyze_trace(tree)
        self.assertEqual(analysis.total_duration_ms, 0.0)
        self.assertEqual(analysis.step_count, 0)
        self.assertEqual(analysis.tool_call_count, 0)

    def test_to_digest_dict(self) -> None:
        events = _build_standard_trace_events()
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        digest = analysis.to_digest_dict()
        self.assertIn("timing", digest)
        self.assertIn("tool_stats", digest)
        self.assertIn("step_breakdown", digest)
        self.assertIn("slowest_operations", digest)
        self.assertIn("subagent_traces", digest)

    def test_tool_stats_sorted_by_total_ms_desc(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.3, loop_index=0, payload={
                "duration_ms": 200, "action_type": "tool_call",
            }),
            _event(3, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 100, "tool_name": "read_file",
            }),
            _event(4, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.8, loop_index=0, payload={
                "duration_ms": 500, "tool_name": "bash",
            }),
            _event(5, RuntimeEventType.STEP_COMPLETED.value, 1.8, loop_index=0, payload={"duration_ms": 800}),
            _event(6, RuntimeEventType.REQUEST_COMPLETED.value, 1.9),
        ]
        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        self.assertEqual(len(analysis.tool_stats), 2)
        self.assertEqual(analysis.tool_stats[0].tool_name, "bash")
        self.assertEqual(analysis.tool_stats[1].tool_name, "read_file")


if __name__ == "__main__":
    unittest.main()

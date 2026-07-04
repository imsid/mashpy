"""Tests for the pure operational-metrics fold over a session's events."""

from __future__ import annotations

import unittest

from mash.evals.metrics import compute_row_metrics
from mash.runtime.events.types import RuntimeEvent


def _ev(agent_id: str, event_type: str, ts: float, **payload: object) -> RuntimeEvent:
    return RuntimeEvent(
        app_id=agent_id,
        agent_id=agent_id,
        event_type=event_type,
        session_id="s1",
        created_at=ts,
        payload=dict(payload),
    )


def _llm(agent_id: str, ts: float, *, i: int, o: int, cr: int = 0, cc: int = 0,
         finish: str = "end_turn") -> RuntimeEvent:
    return _ev(
        agent_id, "llm.request.complete", ts,
        input_tokens=i, output_tokens=o,
        cache_read_input_tokens=cr, cache_creation_input_tokens=cc,
        finish_reason=finish,
    )


class ComputeRowMetricsTests(unittest.TestCase):
    def _session(self) -> list[RuntimeEvent]:
        # Primary "pilot" invokes subagent "helper"; helper hits its step limit.
        return [
            _ev("pilot", "runtime.request.accepted", 100.0),
            _ev("pilot", "runtime.llm.think.started", 100.1),  # ignored
            _llm("pilot", 101.0, i=300, o=50, cr=1000),
            _ev("pilot", "runtime.tool.call.started", 101.5, tool_name="InvokeSubagent"),
            _ev("helper", "runtime.request.accepted", 101.6),
            _llm("helper", 102.0, i=4000, o=200, finish="tool_use"),
            _ev("helper", "runtime.tool.call.started", 102.1, tool_name="bash"),
            _ev("helper", "runtime.tool.call.started", 102.2, tool_name="bash"),
            _ev("helper", "runtime.step.completed", 102.3),
            _ev("helper", "runtime.request.completed", 102.4,
                response_metadata={"stop_reason": "max_steps"}),
            _ev("pilot", "runtime.step.completed", 103.0),
            _llm("pilot", 103.5, i=350, o=90, cr=1200),
            _ev("pilot", "runtime.step.completed", 104.0),
            _ev("pilot", "runtime.request.completed", 105.0,
                response_metadata={"host": {}}),
        ]

    def test_totals_span_primary_and_subagents(self) -> None:
        m = compute_row_metrics(self._session(), primary_agent_id="pilot")
        self.assertEqual(m.llm_calls, 3)           # 2 pilot + 1 helper
        self.assertEqual(m.steps, 3)               # 2 pilot + 1 helper
        self.assertEqual(m.tool_calls, 3)          # 1 pilot + 2 helper
        self.assertEqual(m.tokens.input, 300 + 350 + 4000)
        self.assertEqual(m.tokens.output, 50 + 90 + 200)
        self.assertEqual(m.tokens.cache_read, 1000 + 1200)
        self.assertEqual(m.tool_call_breakdown, {"InvokeSubagent": 1, "bash": 2})

    def test_wall_clock_latency(self) -> None:
        m = compute_row_metrics(self._session(), primary_agent_id="pilot")
        self.assertAlmostEqual(m.latency_ms, 5000.0)  # 100.0 -> 105.0

    def test_subagent_breakdown_and_stop_reason(self) -> None:
        m = compute_row_metrics(self._session(), primary_agent_id="pilot")
        self.assertEqual(m.num_subagent_steps, 1)
        self.assertEqual(len(m.subagents), 1)
        sub = m.subagents[0]
        self.assertEqual(sub.agent_id, "helper")
        self.assertEqual(sub.stop_reason, "max_steps")
        self.assertEqual(sub.steps, 1)
        self.assertEqual(sub.tokens.input, 4000)
        # Primary's stop_reason falls back to its last LLM finish_reason.
        self.assertEqual(m.stop_reason, "end_turn")

    def test_failed_primary_reports_error_and_partial_tokens(self) -> None:
        events = [
            _ev("pilot", "runtime.request.accepted", 200.0),
            _llm("pilot", 201.0, i=100, o=10),
            _ev("pilot", "runtime.request.failed", 202.0, error="boom"),
        ]
        m = compute_row_metrics(events, primary_agent_id="pilot")
        self.assertEqual(m.stop_reason, "error")
        self.assertEqual(m.tokens.input, 100)   # spend before the failure is kept
        self.assertEqual(m.subagents, [])

    def test_empty_events(self) -> None:
        m = compute_row_metrics([], primary_agent_id="pilot")
        self.assertIsNone(m.latency_ms)
        self.assertEqual(m.steps, 0)
        self.assertIsNone(m.stop_reason)


if __name__ == "__main__":
    unittest.main()

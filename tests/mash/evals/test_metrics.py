"""Tests for the pure operational-metrics fold over a session's events."""

from __future__ import annotations

import unittest

from mash.evals.metrics import METRIC_EVENT_TYPES, compute_row_metrics
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
        agent_id, "chat.end", ts,
        status="ok",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": i,
            "gen_ai.usage.output_tokens": o,
            "gen_ai.usage.cache_read.input_tokens": cr,
            "gen_ai.usage.cache_creation.input_tokens": cc,
            "gen_ai.response.finish_reasons": [finish],
        },
    )


def _tool(agent_id: str, ts: float, name: str) -> RuntimeEvent:
    return _ev(agent_id, "execute_tool.start", ts, attributes={"gen_ai.tool.name": name})


class ComputeRowMetricsTests(unittest.TestCase):
    def _session(self) -> list[RuntimeEvent]:
        # Primary "pilot" invokes subagent "helper"; helper hits its step limit.
        return [
            _ev("pilot", "invoke_agent.start", 100.0),
            _ev("pilot", "chat.start", 100.1),  # ignored
            _llm("pilot", 101.0, i=300, o=50, cr=1000),
            _tool("pilot", 101.5, "RunSubAgents"),
            _ev("helper", "invoke_agent.start", 101.6),
            _llm("helper", 102.0, i=4000, o=200, finish="tool_use"),
            _tool("helper", 102.1, "bash"),
            _tool("helper", 102.2, "bash"),
            _ev("helper", "mash.step.end", 102.3),
            # Budget exhaustion fails the request and names its own reason.
            _ev("helper", "invoke_agent.end", 102.4, status="error",
                error="Stopped after reaching the max step limit (1) before finishing.",
                error_code="max_steps_exhausted",
                stop_reason="max_steps"),
            _ev("pilot", "mash.step.end", 103.0),
            _llm("pilot", 103.5, i=350, o=90, cr=1200),
            _ev("pilot", "mash.step.end", 104.0),
            _ev("pilot", "invoke_agent.end", 105.0, status="ok",
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
        self.assertEqual(m.tool_call_breakdown, {"RunSubAgents": 1, "bash": 2})

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
            _ev("pilot", "invoke_agent.start", 200.0),
            _llm("pilot", 201.0, i=100, o=10),
            _ev("pilot", "invoke_agent.end", 202.0, status="error", error="boom"),
        ]
        m = compute_row_metrics(events, primary_agent_id="pilot")
        self.assertEqual(m.stop_reason, "error")
        self.assertEqual(m.tokens.input, 100)   # spend before the failure is kept
        self.assertEqual(m.subagents, [])

    def test_failure_that_names_a_stop_reason_keeps_it(self) -> None:
        """An exhausted step budget must stay distinguishable from any error."""
        events = [
            _ev("pilot", "invoke_agent.start", 200.0),
            _llm("pilot", 201.0, i=100, o=10, finish="tool_use"),
            _ev("pilot", "invoke_agent.end", 202.0, status="error",
                error="Stopped after reaching the max step limit (1) before finishing.",
                error_code="max_steps_exhausted",
                stop_reason="max_steps"),
        ]
        m = compute_row_metrics(events, primary_agent_id="pilot")
        self.assertEqual(m.stop_reason, "max_steps")

    def test_cancelled_primary_reports_cancelled_not_a_finish_reason(self) -> None:
        """A cancelled run must not report the last LLM finish reason.

        A cancelled request's end must decide the stop reason; otherwise the
        row falls through to ``end_turn``, indistinguishable from a run that
        ended on its own.
        """
        events = [
            _ev("pilot", "invoke_agent.start", 200.0),
            _llm("pilot", 201.0, i=100, o=10, finish="end_turn"),
            _ev("pilot", "invoke_agent.end", 202.0, status="cancelled"),
        ]
        m = compute_row_metrics(events, primary_agent_id="pilot")
        self.assertEqual(m.stop_reason, "cancelled")
        self.assertEqual(m.tokens.input, 100)  # spend before the cancel is kept

    def test_cancelled_then_resumed_to_completion_reports_the_outcome(self) -> None:
        """Latest terminal event wins, and by timestamp rather than list order."""
        events = [
            _ev("pilot", "invoke_agent.start", 300.0),
            _ev("pilot", "invoke_agent.end", 301.0, status="cancelled"),
            _llm("pilot", 302.0, i=100, o=10, finish="tool_use"),
            _ev("pilot", "invoke_agent.end", 303.0, status="ok",
                response_metadata={"stop_reason": "end_turn"}),
        ]
        self.assertEqual(
            compute_row_metrics(events, primary_agent_id="pilot").stop_reason,
            "end_turn",
        )
        # Same events, shuffled: the fold is order-independent as documented.
        self.assertEqual(
            compute_row_metrics(
                list(reversed(events)), primary_agent_id="pilot"
            ).stop_reason,
            "end_turn",
        )

    def test_cancelled_events_are_loaded_for_metrics(self) -> None:
        self.assertIn("invoke_agent.end", METRIC_EVENT_TYPES)

    def test_empty_events(self) -> None:
        m = compute_row_metrics([], primary_agent_id="pilot")
        self.assertIsNone(m.latency_ms)
        self.assertEqual(m.steps, 0)
        self.assertIsNone(m.stop_reason)


if __name__ == "__main__":
    unittest.main()

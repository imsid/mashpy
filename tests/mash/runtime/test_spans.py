"""Tests for the span tree builder."""

from __future__ import annotations

import unittest

from mash.runtime.events import build_span_tree, Span, SpanKind, TraceSpanTree
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


class SpanTreeBuilderTests(unittest.TestCase):
    def test_empty_events_returns_empty_tree(self) -> None:
        tree = build_span_tree([])
        self.assertEqual(tree.root.kind, SpanKind.TRACE)
        self.assertEqual(tree.root.duration_ms, 0.0)
        self.assertEqual(tree.span_count, 1)

    def test_minimal_trace_with_one_step(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.TRACE_STARTED.value, 1.05, payload={"message": "hello"}),
            _event(3, RuntimeEventType.CONTEXT_LOADED.value, 1.10),
            _event(4, RuntimeEventType.LLM_THINK_STARTED.value, 1.10, loop_index=0),
            _event(5, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.60, loop_index=0, payload={
                "duration_ms": 500, "action_type": "response", "token_usage": {"input": 100, "output": 50},
            }),
            _event(6, RuntimeEventType.STEP_COMPLETED.value, 1.60, loop_index=0, payload={"duration_ms": 500}),
            _event(7, RuntimeEventType.REQUEST_COMPLETED.value, 1.65),
        ]
        tree = build_span_tree(events)

        self.assertEqual(tree.trace_id, "t-1")
        self.assertEqual(tree.target_agent_id, "agent-1")
        self.assertEqual(tree.root.kind, SpanKind.TRACE)
        self.assertEqual(tree.root.status, "completed")
        self.assertAlmostEqual(tree.root.duration_ms, 650.0, places=0)

        cold_starts = [c for c in tree.root.children if c.kind == SpanKind.COLD_START]
        self.assertEqual(len(cold_starts), 1)
        self.assertAlmostEqual(cold_starts[0].duration_ms, 50.0, places=0)

        ctx_loads = [c for c in tree.root.children if c.kind == SpanKind.CONTEXT_LOAD]
        self.assertEqual(len(ctx_loads), 1)
        self.assertAlmostEqual(ctx_loads[0].duration_ms, 50.0, places=0)

        steps = [c for c in tree.root.children if c.kind == SpanKind.STEP]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].loop_index, 0)

        thinks = [c for c in steps[0].children if c.kind == SpanKind.THINK]
        self.assertEqual(len(thinks), 1)
        self.assertEqual(thinks[0].duration_ms, 500.0)
        self.assertEqual(thinks[0].attributes["token_usage"]["input"], 100)

    def test_step_with_tool_calls(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 400, "action_type": "tool_call",
                "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}],
            }),
            _event(3, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.8, loop_index=0, payload={
                "duration_ms": 200, "tool_name": "bash",
            }),
            _event(4, RuntimeEventType.TOOL_CALL_COMPLETED.value, 2.0, loop_index=0, payload={
                "duration_ms": 150, "tool_name": "read_file",
            }),
            _event(5, RuntimeEventType.STEP_COMPLETED.value, 2.0, loop_index=0, payload={"duration_ms": 1000}),
            _event(6, RuntimeEventType.REQUEST_COMPLETED.value, 2.1),
        ]
        tree = build_span_tree(events)
        step = [c for c in tree.root.children if c.kind == SpanKind.STEP][0]

        tool_calls = [c for c in step.children if c.kind == SpanKind.TOOL_CALL]
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0].name, "Tool: bash")
        self.assertEqual(tool_calls[0].duration_ms, 200.0)
        self.assertEqual(tool_calls[1].name, "Tool: read_file")
        self.assertEqual(tool_calls[1].duration_ms, 150.0)

    def test_subagent_call_creates_subagent_span(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 400, "action_type": "tool_call",
            }),
            _event(3, RuntimeEventType.SUBAGENT_CALL_COMPLETED.value, 3.0, loop_index=0, payload={
                "duration_ms": 1200, "tool_name": "InvokeSubagent",
                "result": {"metadata": {"agent_id": "research", "subagent_session_id": "sub:research:abc", "request_id": "req-123"}},
            }),
            _event(4, RuntimeEventType.STEP_COMPLETED.value, 3.0, loop_index=0, payload={"duration_ms": 2000}),
            _event(5, RuntimeEventType.REQUEST_COMPLETED.value, 3.1),
        ]
        tree = build_span_tree(events)
        step = [c for c in tree.root.children if c.kind == SpanKind.STEP][0]

        subagent_calls = [c for c in step.children if c.kind == SpanKind.SUBAGENT_CALL]
        self.assertEqual(len(subagent_calls), 1)
        self.assertEqual(subagent_calls[0].name, "Subagent: research")
        self.assertEqual(subagent_calls[0].duration_ms, 1200.0)
        self.assertEqual(subagent_calls[0].attributes["agent_id"], "research")
        self.assertEqual(subagent_calls[0].attributes["subagent_session_id"], "sub:research:abc")

    def test_in_progress_trace_without_terminal_event(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 2.0, loop_index=0, payload={
                "duration_ms": 500, "action_type": "tool_call",
            }),
        ]
        tree = build_span_tree(events)
        self.assertEqual(tree.root.status, "in_progress")
        self.assertAlmostEqual(tree.root.duration_ms, 1000.0, places=0)

    def test_error_trace(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.REQUEST_FAILED.value, 2.0, payload={"error": "boom"}),
        ]
        tree = build_span_tree(events)
        self.assertEqual(tree.root.status, "error")

    def test_no_cold_start_when_timestamps_equal(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.TRACE_STARTED.value, 1.0),
            _event(3, RuntimeEventType.REQUEST_COMPLETED.value, 2.0),
        ]
        tree = build_span_tree(events)
        cold_starts = [c for c in tree.root.children if c.kind == SpanKind.COLD_START]
        self.assertEqual(len(cold_starts), 0)

    def test_missing_think_started_uses_synthetic_start(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={
                "duration_ms": 300, "action_type": "response",
            }),
            _event(3, RuntimeEventType.STEP_COMPLETED.value, 1.5, loop_index=0, payload={"duration_ms": 400}),
            _event(4, RuntimeEventType.REQUEST_COMPLETED.value, 1.6),
        ]
        tree = build_span_tree(events)
        step = [c for c in tree.root.children if c.kind == SpanKind.STEP][0]
        thinks = [c for c in step.children if c.kind == SpanKind.THINK]
        self.assertEqual(len(thinks), 1)
        self.assertEqual(thinks[0].duration_ms, 300.0)
        self.assertAlmostEqual(thinks[0].start_time, 1.2, places=1)

    def test_multi_step_trace(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={"duration_ms": 400, "action_type": "tool_call"}),
            _event(3, RuntimeEventType.TOOL_CALL_COMPLETED.value, 1.8, loop_index=0, payload={"duration_ms": 200, "tool_name": "bash"}),
            _event(4, RuntimeEventType.STEP_COMPLETED.value, 1.8, loop_index=0, payload={"duration_ms": 800}),
            _event(5, RuntimeEventType.LLM_THINK_COMPLETED.value, 2.5, loop_index=1, payload={"duration_ms": 500, "action_type": "response"}),
            _event(6, RuntimeEventType.STEP_COMPLETED.value, 2.5, loop_index=1, payload={"duration_ms": 500}),
            _event(7, RuntimeEventType.REQUEST_COMPLETED.value, 2.6),
        ]
        tree = build_span_tree(events)
        steps = [c for c in tree.root.children if c.kind == SpanKind.STEP]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].loop_index, 0)
        self.assertEqual(steps[1].loop_index, 1)

    def test_span_ids_are_deterministic(self) -> None:
        events = [
            _event(1, RuntimeEventType.REQUEST_ACCEPTED.value, 1.0),
            _event(2, RuntimeEventType.TRACE_STARTED.value, 1.05),
            _event(3, RuntimeEventType.LLM_THINK_COMPLETED.value, 1.5, loop_index=0, payload={"duration_ms": 400, "action_type": "tool_call"}),
            _event(4, RuntimeEventType.STEP_COMPLETED.value, 1.5, loop_index=0, payload={"duration_ms": 400}),
            _event(5, RuntimeEventType.REQUEST_COMPLETED.value, 1.6),
        ]
        tree1 = build_span_tree(events)
        tree2 = build_span_tree(events)
        self.assertEqual(tree1.root.span_id, tree2.root.span_id)
        self.assertEqual(
            set(tree1.spans_by_id.keys()),
            set(tree2.spans_by_id.keys()),
        )


if __name__ == "__main__":
    unittest.main()

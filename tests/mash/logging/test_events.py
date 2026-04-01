"""Tests for structured event normalization and validation."""

from __future__ import annotations

import unittest

from mash.logging.events import AgentTraceEvent, MCPEvent, normalize_log_event


class LogEventNormalizationTests(unittest.TestCase):
    def test_mcp_tool_error_normalizes_with_required_fields(self) -> None:
        event = MCPEvent(
            event_type="mcp.tool.error",
            app_id="primary",
            session_id="session-1",
            server_name="demo",
            tool_name="lookup",
            duration_ms=12,
            error="boom",
            trace_id="trace-1",
        )

        normalized = normalize_log_event(event)

        self.assertEqual(normalized["event_type"], "mcp.tool.error")
        self.assertEqual(normalized["trace_id"], "trace-1")
        self.assertEqual(normalized["payload"]["server_name"], "demo")
        self.assertEqual(normalized["payload"]["tool_name"], "lookup")
        self.assertEqual(normalized["payload"]["duration_ms"], 12)
        self.assertEqual(normalized["payload"]["error"], "boom")

    def test_subagent_events_normalize_as_agent_trace_events(self) -> None:
        event = AgentTraceEvent(
            event_type="subagent.request.started",
            app_id="primary",
            session_id="session-1",
            trace_id="trace-1",
            payload={
                "agent_id": "research",
                "request_id": "req-1",
                "event": "request.started",
            },
        )

        normalized = normalize_log_event(event)

        self.assertEqual(normalized["event_class"], "AgentTraceEvent")
        self.assertEqual(normalized["event_type"], "subagent.request.started")
        self.assertEqual(normalized["trace_id"], "trace-1")
        self.assertEqual(normalized["payload"]["payload"]["agent_id"], "research")
        self.assertEqual(normalized["payload"]["payload"]["request_id"], "req-1")


if __name__ == "__main__":
    unittest.main()

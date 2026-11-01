"""Tests for structured event normalization and validation."""

from __future__ import annotations

import unittest

from mash.logging.events import LLMEvent, MCPEvent, normalize_log_event


class LogEventNormalizationTests(unittest.TestCase):
    def test_mcp_client_error_normalizes_with_required_fields(self) -> None:
        event = MCPEvent(
            event_type="mcp.client.error",
            app_id="primary",
            session_id="session-1",
            server_name="demo",
            error="boom",
            trace_id="trace-1",
        )

        normalized = normalize_log_event(event)

        self.assertEqual(normalized["event_type"], "mcp.client.error")
        self.assertEqual(normalized["trace_id"], "trace-1")
        self.assertEqual(normalized["payload"]["server_name"], "demo")
        self.assertEqual(normalized["payload"]["error"], "boom")

    def test_chat_delta_normalizes_with_its_chunk(self) -> None:
        event = LLMEvent(
            event_type="chat.delta",
            app_id="primary",
            session_id="session-1",
            trace_id="trace-1",
            provider="anthropic",
            model="claude",
            payload={"text": "hi", "index": 0},
        )

        normalized = normalize_log_event(event)

        self.assertEqual(normalized["event_class"], "LLMEvent")
        self.assertEqual(normalized["payload"]["payload"], {"text": "hi", "index": 0})
        self.assertEqual(normalized["payload"]["provider"], "anthropic")

    def test_mcp_event_requires_a_server_name(self) -> None:
        with self.assertRaises(ValueError):
            normalize_log_event(
                MCPEvent(event_type="mcp.client.error", app_id="primary", session_id=None)
            )


if __name__ == "__main__":
    unittest.main()

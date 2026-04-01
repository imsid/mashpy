"""Tests for MCP manager event emission."""

from __future__ import annotations

import unittest

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager


class _RecordingEventLogger:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


class _FailingToolServer:
    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        del tool_name, arguments
        raise RuntimeError("tool boom")


class MCPManagerEventTests(unittest.TestCase):
    def test_add_server_connection_failure_emits_client_error(self) -> None:
        logger = _RecordingEventLogger()
        manager = MCPManager(
            app_id="primary",
            event_logger=logger,
            session_id="session-1",
        )

        def _raise_client_error(url: str, name: str, headers=None):
            del url, name, headers
            raise MCPClientError("connect boom")

        manager._host.get_client = _raise_client_error  # type: ignore[method-assign]

        with self.assertRaisesRegex(MCPClientError, "Failed to connect"):
            manager.add_server("demo", "https://example.test")

        event_types = [event.event_type for event in logger.events]
        self.assertEqual(event_types, ["mcp.client.connect", "mcp.client.error"])

    def test_tool_invocation_failure_emits_tool_error(self) -> None:
        logger = _RecordingEventLogger()
        manager = MCPManager(
            app_id="primary",
            event_logger=logger,
            session_id="session-1",
        )
        manager._servers["demo"] = _FailingToolServer()  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "tool boom"):
            manager.call_tool("demo", "lookup", {"x": 1})

        event_types = [event.event_type for event in logger.events]
        self.assertEqual(event_types, ["mcp.tool.call", "mcp.tool.error"])
        self.assertEqual(logger.events[-1].tool_name, "lookup")
        self.assertEqual(logger.events[-1].error, "tool boom")


if __name__ == "__main__":
    unittest.main()

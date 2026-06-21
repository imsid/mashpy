"""Tests for MCP manager event emission."""

from __future__ import annotations

import unittest

from mash.logging.trace_context import bound_session_id
from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager


class _OkToolServer:
    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        del tool_name, arguments
        return {"ok": True}


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

    def test_tool_events_use_bound_request_session(self) -> None:
        # The manager is constructed once with a default session, but events must
        # follow the in-flight request's session (e.g. a workflow's per-run one).
        logger = _RecordingEventLogger()
        manager = MCPManager(
            app_id="primary",
            event_logger=logger,
            session_id="construction-session",
        )
        manager._servers["demo"] = _OkToolServer()  # type: ignore[assignment]

        with bound_session_id("request-session-42"):
            manager.call_tool("demo", "lookup", {"x": 1})

        self.assertEqual(
            [e.event_type for e in logger.events],
            ["mcp.tool.call", "mcp.tool.result"],
        )
        self.assertTrue(
            all(e.session_id == "request-session-42" for e in logger.events)
        )

    def test_tool_events_fall_back_to_construction_session(self) -> None:
        logger = _RecordingEventLogger()
        manager = MCPManager(
            app_id="primary",
            event_logger=logger,
            session_id="construction-session",
        )
        manager._servers["demo"] = _OkToolServer()  # type: ignore[assignment]

        manager.call_tool("demo", "lookup", {"x": 1})

        self.assertTrue(
            all(e.session_id == "construction-session" for e in logger.events)
        )


if __name__ == "__main__":
    unittest.main()

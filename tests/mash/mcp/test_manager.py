"""Tests for MCP manager event emission."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig


class _OkToolServer:
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        del tool_name, arguments
        return {"ok": True}


class _FailingToolServer:
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        del tool_name, arguments
        raise RuntimeError("tool boom")


class _RecordingEventLogger:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class MCPManagerEventTests(unittest.TestCase):
    def test_add_server_connection_failure_emits_client_error(self) -> None:
        logger = _RecordingEventLogger()
        manager = MCPManager(app_id="primary", event_logger=logger)

        with patch(
            "mash.mcp.manager.MCPHTTPClient.connect",
            side_effect=MCPClientError("connect boom"),
        ):
            with self.assertRaisesRegex(MCPClientError, "Failed to connect"):
                asyncio.run(
                    manager.add_server(MCPServerConfig(name="demo", url="https://example.test"))
                )

        event_types = [event.event_type for event in logger.events]
        self.assertEqual(event_types, ["mcp.client.connect", "mcp.client.error"])

    def test_tool_calls_write_no_events(self) -> None:
        # A tool call is the request's execute_tool span; the manager only
        # records its connection lifecycle.
        logger = _RecordingEventLogger()
        manager = MCPManager(app_id="primary", event_logger=logger)
        manager._servers["demo"] = _OkToolServer()  # type: ignore[assignment]
        manager._servers["broken"] = _FailingToolServer()  # type: ignore[assignment]

        self.assertEqual(
            asyncio.run(manager.call_tool("demo", "lookup", {"x": 1})), {"ok": True}
        )
        with self.assertRaisesRegex(RuntimeError, "tool boom"):
            asyncio.run(manager.call_tool("broken", "lookup", {"x": 1}))
        self.assertEqual(logger.events, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for MCP HTTP client async bridging from sync APIs."""

from __future__ import annotations

import asyncio
import unittest

from mash.mcp.client import MCPClientError, MCPHTTPClient, RPCResponse


class MCPHTTPClientAsyncBridgeTests(unittest.TestCase):
    def test_run_awaitable_without_running_loop(self) -> None:
        client = MCPHTTPClient.__new__(MCPHTTPClient)

        async def _value() -> int:
            return 7

        self.assertEqual(client._run_awaitable(_value()), 7)

    def test_run_awaitable_with_running_loop(self) -> None:
        client = MCPHTTPClient.__new__(MCPHTTPClient)

        async def _runner() -> None:
            async def _value() -> int:
                return 11

            self.assertEqual(client._run_awaitable(_value()), 11)

        asyncio.run(_runner())

    def test_list_tools_with_running_loop(self) -> None:
        client = MCPHTTPClient.__new__(MCPHTTPClient)
        handled: list[RPCResponse] = []

        def _make_request(method: str) -> RPCResponse:
            self.assertEqual(method, "tools/list")
            return RPCResponse(
                result={"tools": [{"name": "alpha"}]},
                elicitation_requests=[],
            )

        async def _handle_interactions(response: RPCResponse) -> None:
            handled.append(response)

        client._make_request = _make_request  # type: ignore[method-assign]
        client._handle_interactions = _handle_interactions  # type: ignore[method-assign]

        async def _runner() -> None:
            tools = client.list_tools()
            self.assertEqual(tools, [{"name": "alpha"}])

        asyncio.run(_runner())
        self.assertEqual(len(handled), 1)

    def test_extract_interactions_rejects_sampling_requests(self) -> None:
        with self.assertRaisesRegex(
            MCPClientError, "deprecated MCP feature 'sampling/createMessage'"
        ):
            MCPHTTPClient._extract_interactions(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "sampling-1",
                        "method": "sampling/createMessage",
                        "params": {},
                    }
                ]
            )

    def test_extract_interactions_keeps_elicitation_requests(self) -> None:
        elicitation = MCPHTTPClient._extract_interactions(
            [
                {
                    "jsonrpc": "2.0",
                    "id": "elicitation-1",
                    "method": "elicitation/createMessage",
                    "params": {"message": "Need input"},
                }
            ]
        )
        self.assertEqual(len(elicitation), 1)
        self.assertEqual(elicitation[0]["id"], "elicitation-1")

    def test_handle_sse_payload_rejects_sampling_requests(self) -> None:
        client = MCPHTTPClient.__new__(MCPHTTPClient)

        async def _runner() -> None:
            with self.assertRaisesRegex(
                MCPClientError, "deprecated MCP feature 'sampling/createMessage'"
            ):
                await client._handle_sse_payload(
                    '{"jsonrpc":"2.0","id":"sampling-1","method":"sampling/createMessage","params":{}}'
                )

        asyncio.run(_runner())

    def test_run_awaitable_re_raises_errors(self) -> None:
        client = MCPHTTPClient.__new__(MCPHTTPClient)

        async def _runner() -> None:
            async def _fail() -> None:
                raise RuntimeError("bridge failure")

            with self.assertRaisesRegex(RuntimeError, "bridge failure"):
                client._run_awaitable(_fail())

        asyncio.run(_runner())

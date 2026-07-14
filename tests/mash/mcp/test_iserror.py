"""Regression tests: MCP ``isError`` surfaces as a failed tool result."""

from __future__ import annotations

import asyncio
import unittest

from mash.runtime.factory import extract_mcp_text
from mash.tools.mcp import MCPToolAdapter


class ExtractMcpTextTests(unittest.TestCase):
    def test_reads_iserror_flag(self) -> None:
        result = {
            "content": [{"type": "text", "text": "upstream 503"}],
            "isError": True,
        }
        text, is_error = extract_mcp_text(result)
        self.assertEqual(text, "upstream 503")
        self.assertTrue(is_error)

    def test_success_result_is_not_error(self) -> None:
        result = {"content": [{"type": "text", "text": "ok"}]}
        text, is_error = extract_mcp_text(result)
        self.assertEqual(text, "ok")
        self.assertFalse(is_error)

    def test_non_dict_result_is_not_error(self) -> None:
        text, is_error = extract_mcp_text("plain")
        self.assertEqual(text, "plain")
        self.assertFalse(is_error)


class MCPToolAdapterErrorTests(unittest.TestCase):
    def _run(self, executor):
        adapter = MCPToolAdapter(
            name="mcp_tool",
            description="",
            parameters={},
            executor=executor,
        )
        return asyncio.run(adapter.execute({}))

    def test_iserror_tuple_yields_error_result(self) -> None:
        server_result = {
            "content": [{"type": "text", "text": "validation failed"}],
            "isError": True,
        }
        out = self._run(lambda args: extract_mcp_text(server_result))
        self.assertTrue(out.is_error)
        self.assertEqual(out.content, "validation failed")

    def test_success_tuple_yields_success_result(self) -> None:
        server_result = {"content": [{"type": "text", "text": "done"}]}
        out = self._run(lambda args: extract_mcp_text(server_result))
        self.assertFalse(out.is_error)
        self.assertEqual(out.content, "done")

    def test_bare_string_is_treated_as_success(self) -> None:
        out = self._run(lambda args: "legacy string")
        self.assertFalse(out.is_error)
        self.assertEqual(out.content, "legacy string")

    def test_raised_exception_yields_error_result(self) -> None:
        def boom(args):
            raise RuntimeError("kaboom")

        out = self._run(boom)
        self.assertTrue(out.is_error)
        self.assertIn("kaboom", out.content)


if __name__ == "__main__":
    unittest.main()

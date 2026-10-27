"""edit_file against the daemon's file reads and writes, through a stand-in relay."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mash.logging.trace_context import set_request_id
from mash.sandbox.daemon import FileNotFound, read_file, write_file
from mash.tools.sandbox import build_sandbox_tools


class EditFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = tempfile.mkdtemp()
        _, self.tool = build_sandbox_tools(
            host_api_url="http://host", token="tok", agent_id="research"
        )
        set_request_id("req-1")

    async def _edit(self, path: str, old_text: str, new_text: str):
        folder = self.folder

        async def relay(request: httpx.Request) -> httpx.Response:
            target = request.url.params["path"]
            if request.method == "PUT":
                await write_file(target, request.content, folder=folder)
                return httpx.Response(200, json={"path": target, "bytes": len(request.content)})
            try:
                return httpx.Response(200, content=await read_file(target, folder=folder))
            except FileNotFound:
                return httpx.Response(
                    404, json={"error": {"code": "FILE_NOT_FOUND", "message": "no file"}}
                )

        client = httpx.AsyncClient
        with patch(
            "mash.tools.sandbox.httpx.AsyncClient",
            lambda **kwargs: client(transport=httpx.MockTransport(relay), **kwargs),
        ):
            return await self.tool.execute(
                {"path": path, "old_text": old_text, "new_text": new_text}
            )

    def _write(self, path: str, data: bytes) -> None:
        with open(os.path.join(self.folder, path), "wb") as fh:
            fh.write(data)

    def _read(self, path: str) -> str:
        with open(os.path.join(self.folder, path), encoding="utf-8") as fh:
            return fh.read()

    async def test_empty_old_text_creates_a_file_and_its_folders(self) -> None:
        result = await self._edit("site/notes.md", "", "# Notes\n")

        self.assertFalse(result.is_error, result.content)
        self.assertEqual(self._read("site/notes.md"), "# Notes\n")

    async def test_it_does_not_create_over_an_existing_file(self) -> None:
        self._write("notes.md", b"keep\n")

        result = await self._edit("notes.md", "", "replaced\n")

        self.assertTrue(result.is_error)
        self.assertIn("already exists", result.content)
        self.assertEqual(self._read("notes.md"), "keep\n")

    async def test_text_that_appears_once_is_replaced(self) -> None:
        self._write("inputs.json", b'{\n  "period": "all"\n}\n')

        result = await self._edit("inputs.json", '"period": "all"', '"period": "last_12_months"')

        self.assertFalse(result.is_error, result.content)
        self.assertEqual(self._read("inputs.json"), '{\n  "period": "last_12_months"\n}\n')

    async def test_text_that_is_not_there_changes_nothing(self) -> None:
        self._write("inputs.json", b'{"period": "all"}\n')

        result = await self._edit("inputs.json", '"period": "ALL"', '"period": "x"')

        self.assertTrue(result.is_error)
        self.assertIn("is not in inputs.json", result.content)
        self.assertEqual(self._read("inputs.json"), '{"period": "all"}\n')

    async def test_text_that_appears_twice_changes_nothing(self) -> None:
        self._write("build.py", b"x = 1\ny = 1\n")

        result = await self._edit("build.py", "= 1", "= 2")

        self.assertTrue(result.is_error)
        self.assertIn("appears 2 times", result.content)
        self.assertEqual(self._read("build.py"), "x = 1\ny = 1\n")

    async def test_a_missing_file_is_not_edited(self) -> None:
        result = await self._edit("missing.txt", "a", "b")

        self.assertTrue(result.is_error)
        self.assertIn("leave old_text empty to create it", result.content)

    async def test_a_binary_file_is_not_edited(self) -> None:
        self._write("chart.png", b"\x89PNG\xff\xfe")

        result = await self._edit("chart.png", "PNG", "JPG")

        self.assertTrue(result.is_error)
        self.assertIn("not a UTF-8 text file", result.content)


if __name__ == "__main__":
    unittest.main()

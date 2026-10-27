"""run_code's command, run by bash as the sandbox daemon runs it."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mash.logging.trace_context import set_request_id
from mash.sandbox.daemon import run_command
from mash.tools.sandbox import build_sandbox_tools


class RunCodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.folder = tempfile.mkdtemp()
        self.tool, _ = build_sandbox_tools(
            host_api_url="http://host", token="tok", agent_id="research"
        )
        set_request_id("req-1")

    async def _run(self, language: str, code: str):
        folder = self.folder

        async def relay(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            result = await run_command(body["command"], folder=folder, stdin=body["stdin"])
            return httpx.Response(
                200, json={"output": result.output, "exit_code": result.exit_code}
            )

        client = httpx.AsyncClient
        with patch(
            "mash.tools.sandbox.httpx.AsyncClient",
            lambda **kwargs: client(transport=httpx.MockTransport(relay), **kwargs),
        ):
            return await self.tool.execute({"language": language, "code": code})

    async def test_shell_stops_at_the_first_command_that_fails(self) -> None:
        result = await self._run("shell", "false\necho after")

        self.assertEqual(result.metadata["exit_code"], 1)
        self.assertNotIn("after", result.content)

    async def test_a_failure_inside_a_pipe_fails_the_call(self) -> None:
        result = await self._run("shell", "false | cat")

        self.assertEqual(result.metadata["exit_code"], 1)

    async def test_python_needs_no_quoting(self) -> None:
        result = await self._run("python", "print('it\\'s \"quoted\" {start}')")

        self.assertEqual(result.content, "it's \"quoted\" {start}")

    def test_the_model_checks_what_is_installed(self) -> None:
        self.assertIn("check with `pip list`", self.tool.description)


if __name__ == "__main__":
    unittest.main()

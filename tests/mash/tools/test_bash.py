"""BashTool posts each command to the host's sandbox relay."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from mash.logging import bound_request_id
from mash.tools import bash as bash_module
from mash.tools.bash import BashTool

_AsyncClient = httpx.AsyncClient


class BashToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = httpx.Response(200, json={"output": "ok", "exit_code": 0, "timed_out": False})

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.reply

        transport = httpx.MockTransport(handler)
        client = patch.object(
            bash_module.httpx,
            "AsyncClient",
            lambda **kwargs: _AsyncClient(transport=transport, **kwargs),
        )
        client.start()
        self.addCleanup(client.stop)
        self.tool = BashTool(host_api_url="http://host:8000/", token="agent-tok")

    def _run(self, args: dict, *, request_id: str | None = "req-1"):
        async def _execute():
            with bound_request_id(request_id):
                return await self.tool.execute(args)

        return asyncio.run(_execute())

    def test_posts_the_command_with_the_request_and_agent_token(self) -> None:
        result = self._run({"command": "ls", "timeout": 60})

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ok")
        [request] = self.requests
        self.assertEqual(str(request.url), "http://host:8000/sandbox/exec")
        self.assertEqual(request.headers["Authorization"], "Bearer agent-tok")
        self.assertEqual(
            json.loads(request.content),
            {"request_id": "req-1", "command": "ls", "timeout": 60},
        )

    def test_nonzero_exit_is_output_not_error(self) -> None:
        self.reply = httpx.Response(200, json={"output": "no", "exit_code": 2, "timed_out": False})
        result = self._run({"command": "false"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.metadata, {"exit_code": 2})

    def test_timeout_is_an_error(self) -> None:
        self.reply = httpx.Response(200, json={"output": "", "exit_code": None, "timed_out": True})
        result = self._run({"command": "sleep 99", "timeout": 1})

        self.assertTrue(result.is_error)
        self.assertIn("timed out after 1 seconds", result.content)

    def test_relay_errors_reach_the_model(self) -> None:
        self.reply = httpx.Response(
            503, json={"error": {"code": "SANDBOX_CAPACITY", "message": "all 20 sandboxes are busy"}}
        )
        result = self._run({"command": "ls"})

        self.assertTrue(result.is_error)
        self.assertIn("all 20 sandboxes are busy", result.content)

    def test_needs_a_request(self) -> None:
        result = self._run({"command": "ls"}, request_id=None)

        self.assertTrue(result.is_error)
        self.assertEqual(self.requests, [])

    def test_rejects_dangerous_commands_and_bad_timeouts_locally(self) -> None:
        for args in (
            {"command": "sudo ls"},
            {"command": ""},
            {"command": "ls", "timeout": 0},
            {"command": "ls", "timeout": "soon"},
        ):
            with self.subTest(args=args):
                self.assertTrue(self._run(args).is_error)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()

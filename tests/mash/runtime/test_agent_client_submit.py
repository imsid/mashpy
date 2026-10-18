"""AgentClient sends H2A 0.4.0 fields and the bearer token."""

from __future__ import annotations

import functools
import unittest
from unittest.mock import patch

import httpx

from mash.runtime.client import AgentClient
from mash.runtime.server import AgentServer

from test_agent_server_submit import _Runtime


class AgentClientSubmitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = _Runtime()
        transport = httpx.ASGITransport(app=AgentServer(self.runtime, token="tok-1").app)
        client_patch = patch(
            "mash.runtime.client.httpx.AsyncClient",
            functools.partial(httpx.AsyncClient, transport=transport),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

    async def test_post_request_sends_new_fields_with_token(self) -> None:
        client = AgentClient("http://agent", "agent-a", token="tok-1")
        parent = {"kind": "workflow", "workflow_id": "changelog", "run_id": "run_9", "task_id": "summarize"}

        request_id = await client.post_request(
            "summarize",
            session_id="s-1",
            request_id="run_9-summarize",
            context="release notes",
            host={"host_id": "assistant", "primary": "agent-a", "subagents": []},
            parent=parent,
        )

        self.assertEqual(request_id, "req-1")
        [submission] = self.runtime.submissions
        self.assertEqual(submission["request_id"], "run_9-summarize")
        self.assertEqual(submission["context"], "release notes")
        self.assertEqual(submission["parent"], parent)

    async def test_resubmitting_a_known_request_id_returns_it(self) -> None:
        self.runtime.known["req-a"] = {"request_id": "req-a", "agent_id": "agent-a", "session_id": "s-1", "status": "accepted"}
        client = AgentClient("http://agent", "agent-a", token="tok-1")

        request_id = await client.post_request("again", session_id="s-1", request_id="req-a")

        self.assertEqual(request_id, "req-a")
        self.assertEqual(self.runtime.submissions, [])

    async def test_describe(self) -> None:
        client = AgentClient("http://agent", "agent-a", token="tok-1")

        described = await client.describe()

        self.assertEqual(described["tools"], [{"name": "bash"}])


    async def test_compact_session(self) -> None:
        client = AgentClient("http://agent", "agent-a", token="tok-1")

        result = await client.compact_session("s-1", reason="manual")

        self.assertEqual(result["summary_text"], "summary")
        self.assertEqual(self.runtime.compactions[0]["session_id"], "s-1")


if __name__ == "__main__":
    unittest.main()

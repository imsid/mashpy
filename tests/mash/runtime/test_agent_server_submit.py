"""AgentServer forwards structured_output and metadata on submit."""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from mash.runtime.server import AgentServer


class _Store:
    async def close(self) -> None:
        return None


class _Runtime:
    app_id = "agent-a"

    def __init__(self) -> None:
        self.runtime_store = _Store()
        self.store = _Store()
        self.submissions: list[dict] = []

    async def open(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def submit_request(self, **kwargs) -> dict:
        self.submissions.append(kwargs)
        return {
            "request_id": "req-1",
            "agent_id": self.app_id,
            "session_id": kwargs["session_id"],
            "status": "accepted",
        }


SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
}


class AgentServerSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _Runtime()
        self.client = TestClient(AgentServer(self.runtime).app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _post(self, body: dict):
        return self.client.post("/agent/agent-a/request", json=body)

    def test_forwards_structured_output_and_metadata(self) -> None:
        resp = self._post(
            {
                "message": "plan it",
                "session_id": "s-1",
                "structured_output": SCHEMA,
                "metadata": {"tenant": "acme"},
            }
        )

        self.assertEqual(resp.status_code, 202)
        [submission] = self.runtime.submissions
        self.assertEqual(submission["metadata"], {"tenant": "acme"})
        self.assertEqual(
            submission["structured_output"],
            {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
                "required": ["summary"],
            },
        )

    def test_omitted_fields_are_forwarded_as_none(self) -> None:
        resp = self._post({"message": "plan it", "session_id": "s-1"})

        self.assertEqual(resp.status_code, 202)
        [submission] = self.runtime.submissions
        self.assertIsNone(submission["structured_output"])
        self.assertIsNone(submission["metadata"])

    def test_invalid_structured_output_returns_400(self) -> None:
        resp = self._post(
            {"message": "plan it", "session_id": "s-1", "structured_output": "summary"}
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_STRUCTURED_OUTPUT")
        self.assertEqual(self.runtime.submissions, [])

    def test_non_object_metadata_returns_400(self) -> None:
        resp = self._post({"message": "plan it", "session_id": "s-1", "metadata": ["acme"]})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(self.runtime.submissions, [])


if __name__ == "__main__":
    unittest.main()

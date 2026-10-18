"""AgentServer submit handling: forwarded fields, idempotency, auth, describe."""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from mash.runtime.requests import RequestIdConflictError
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
        self.known: dict[str, dict] = {}
        self.compactions: list[dict] = []

    async def open(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def get_session_info(self) -> dict:
        return {}

    async def get_accepted_request(self, request_id: str) -> dict | None:
        if request_id == "other-agents":
            raise RequestIdConflictError("belongs to another agent")
        return self.known.get(request_id)

    def describe(self) -> dict:
        return {"agent_id": self.app_id, "tools": [{"name": "bash"}], "skills": []}

    async def compact_session(self, **kwargs) -> tuple:
        self.compactions.append(kwargs)
        return "summary", "trace-c"

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
        self.assertIsNone(submission["request_id"])
        self.assertIsNone(submission["context"])
        self.assertIsNone(submission["host_snapshot"])
        self.assertIsNone(submission["parent"])
        self.assertIsNone(submission["user_id"])

    def test_forwards_user_id(self) -> None:
        resp = self._post({"message": "plan it", "session_id": "s-1", "user_id": "u@acme.io"})

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.runtime.submissions[0]["user_id"], "u@acme.io")

    def test_invalid_user_id_returns_400(self) -> None:
        for user_id in ("", "has space", 42, "x" * 256):
            resp = self._post({"message": "plan it", "session_id": "s-1", "user_id": user_id})
            self.assertEqual(resp.status_code, 400, user_id)
        self.assertEqual(self.runtime.submissions, [])

    def test_workflow_parent_with_user_id_returns_400(self) -> None:
        parent = {"kind": "workflow", "workflow_id": "wf", "run_id": "r", "task_id": "t"}
        resp = self._post(
            {"message": "step", "session_id": "s-1", "user_id": "u-1", "parent": parent}
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.runtime.submissions, [])

    def test_forwards_request_id_context_host_and_parent(self) -> None:
        host = {"host_id": "assistant", "primary": "concierge", "subagents": ["agent-a"]}
        parent = {"kind": "subagent", "request_id": "req_1", "agent_id": "concierge", "session_id": "s-1"}
        resp = self._post(
            {
                "message": "find papers",
                "session_id": "s-1",
                "request_id": "req_1-sub-0-0",
                "context": "user is in Berlin",
                "host": host,
                "parent": parent,
            }
        )

        self.assertEqual(resp.status_code, 202)
        [submission] = self.runtime.submissions
        self.assertEqual(submission["request_id"], "req_1-sub-0-0")
        self.assertEqual(submission["context"], "user is in Berlin")
        self.assertEqual(submission["host_snapshot"], host)
        self.assertEqual(submission["parent"], parent)

    def test_known_request_id_returns_200_without_submitting(self) -> None:
        existing = {"request_id": "req-a", "agent_id": "agent-a", "session_id": "s-1", "status": "accepted"}
        self.runtime.known["req-a"] = existing

        resp = self._post({"message": "plan it", "session_id": "s-1", "request_id": "req-a"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), existing)
        self.assertEqual(self.runtime.submissions, [])

    def test_request_id_of_another_agent_returns_409(self) -> None:
        resp = self._post({"message": "plan it", "session_id": "s-1", "request_id": "other-agents"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "REQUEST_ID_CONFLICT")

    def test_invalid_request_id_returns_400(self) -> None:
        resp = self._post({"message": "plan it", "session_id": "s-1", "request_id": "has space"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")

    def test_invalid_parent_returns_400(self) -> None:
        resp = self._post(
            {"message": "plan it", "session_id": "s-1", "parent": {"kind": "subagent"}}
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_PARENT")
        self.assertEqual(self.runtime.submissions, [])

    def test_non_string_context_and_non_object_host_return_400(self) -> None:
        for body in ({"context": 3}, {"host": ["assistant"]}):
            with self.subTest(body=body):
                resp = self._post({"message": "plan it", "session_id": "s-1", **body})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")

    def test_compact_runs_on_the_runtime(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/session/s-1/compact",
            json={"reason": "manual", "session_total_tokens_reset": 10},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"summary_text": "summary", "trace_id": "trace-c"})
        self.assertEqual(
            self.runtime.compactions,
            [{"session_id": "s-1", "reason": "manual", "session_total_tokens_reset": 10}],
        )

    def test_compact_rejects_invalid_reset(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/session/s-1/compact",
            json={"session_total_tokens_reset": -1},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.runtime.compactions, [])

    def test_describe_returns_tools_and_skills(self) -> None:
        resp = self.client.get("/agent/agent-a/describe")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tools"], [{"name": "bash"}])
        self.assertEqual(self.client.get("/agent/other/describe").status_code, 404)

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


class AgentServerAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _Runtime()
        self.client = TestClient(AgentServer(self.runtime, token="tok-1").app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_missing_or_wrong_token_returns_401(self) -> None:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "tok-1"}):
            with self.subTest(headers=headers):
                resp = self.client.post(
                    "/agent/agent-a/request",
                    json={"message": "plan it", "session_id": "s-1"},
                    headers=headers,
                )
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.json()["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(self.runtime.submissions, [])

    def test_valid_token_is_accepted(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/request",
            json={"message": "plan it", "session_id": "s-1"},
            headers={"Authorization": "Bearer tok-1"},
        )

        self.assertEqual(resp.status_code, 202)

    def test_health_needs_no_token(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()

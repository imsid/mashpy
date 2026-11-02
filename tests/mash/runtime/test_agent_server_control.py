"""AgentServer serves the control routes AgentClient calls."""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from mash.runtime.engine import InteractionClosedError
from mash.runtime.errors import RequestStaleError
from mash.runtime.server import AgentServer


class _Store:
    async def close(self) -> None:
        return None

    async def has_request(self, request_id: str) -> bool:
        return request_id != "missing"


class _Runtime:
    app_id = "agent-a"

    def __init__(self) -> None:
        self.runtime_store = _Store()
        self.store = _Store()
        self.calls: list[tuple[str, str]] = []
        self.answers: list[tuple[str, str, object]] = []

    async def open(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def _record(self, name: str, request_id: str) -> dict:
        self.calls.append((name, request_id))
        if request_id == "missing":
            raise KeyError(request_id)
        if request_id == "stale":
            raise RequestStaleError("session has newer turns")
        return {"request_id": request_id, "op": name}

    async def get_request_status(self, request_id: str) -> dict:
        return await self._record("status", request_id)

    async def resume_request(self, request_id: str) -> dict:
        return await self._record("resume", request_id)

    async def cancel_request(self, request_id: str) -> dict:
        return await self._record("cancel", request_id)

    async def rerun_request(self, request_id: str) -> dict:
        return await self._record("rerun", request_id)

    async def answer_interaction(
        self, request_id: str, interaction_id: str, response: object
    ) -> None:
        if interaction_id == "int-missing":
            raise KeyError(interaction_id)
        if interaction_id == "int-closed":
            raise InteractionClosedError("interaction 'int-closed' is no longer open")
        self.answers.append((request_id, interaction_id, response))


ROUTES = [
    ("GET", "status"),
    ("POST", "resume"),
    ("POST", "cancel"),
    ("POST", "rerun"),
]


class AgentServerControlRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _Runtime()
        self.client = TestClient(AgentServer(self.runtime).app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_routes_call_the_runtime_and_return_its_result(self) -> None:
        for method, op in ROUTES:
            with self.subTest(op=op):
                resp = self.client.request(method, f"/agent/agent-a/request/req-1/{op}")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"request_id": "req-1", "op": op})
        self.assertEqual(
            self.runtime.calls,
            [("status", "req-1"), ("resume", "req-1"), ("cancel", "req-1"), ("rerun", "req-1")],
        )

    def test_unknown_request_returns_404(self) -> None:
        for method, op in ROUTES:
            with self.subTest(op=op):
                resp = self.client.request(method, f"/agent/agent-a/request/missing/{op}")
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json()["error"]["code"], "REQUEST_NOT_FOUND")

    def test_stale_resume_returns_409(self) -> None:
        resp = self.client.post("/agent/agent-a/request/stale/resume")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "REQUEST_STALE")

    def test_other_agent_id_returns_route_not_found(self) -> None:
        resp = self.client.get("/agent/agent-b/request/req-1/status")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "ROUTE_NOT_FOUND")
        self.assertEqual(self.runtime.calls, [])

    def test_interaction_answer_goes_to_the_runtime(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/request/req-1/interaction",
            json={"interaction_id": "int-1", "response": "yes"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "interaction_id": "int-1"})
        self.assertEqual(self.runtime.answers, [("req-1", "int-1", "yes")])

    def test_answer_to_a_closed_interaction_returns_409(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/request/req-1/interaction",
            json={"interaction_id": "int-closed", "response": "yes"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "INTERACTION_CLOSED")

    def test_answer_to_an_unknown_interaction_returns_404(self) -> None:
        resp = self.client.post(
            "/agent/agent-a/request/req-1/interaction",
            json={"interaction_id": "int-missing", "response": "yes"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "INTERACTION_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()

"""AgentServer serves the control routes AgentClient calls."""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from mash.runtime.errors import RequestStaleError
from mash.runtime.server import AgentServer


class _Store:
    async def close(self) -> None:
        return None


class _Runtime:
    app_id = "agent-a"

    def __init__(self) -> None:
        self.runtime_store = _Store()
        self.store = _Store()
        self.calls: list[tuple[str, str]] = []

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


if __name__ == "__main__":
    unittest.main()

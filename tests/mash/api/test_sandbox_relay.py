"""The host's sandbox relay, and bash end to end through it."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from starlette.testclient import TestClient

from conftest import build_test_stores
from mash.api.routes.sandbox import build_sandbox_router
from mash.runtime import HostBuilder
from mash.runtime.events import RequestStatus, RuntimeEvent, RuntimeEventType
from mash.runtime.host.tokens import derive_agent_token
from mash.sandbox.manager import SandboxCapacityError, SandboxManager
from mash.testing.runtime_fixtures import build_tool_calling_spec, metadata
from mash.testing.sandboxes import LocalSandboxProvider

SECRET = "host-secret"


class _RecordingSandboxes:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def exec(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.calls.append({"user_id": user_id, **kwargs})
        return {"output": "ok", "exit_code": 0, "timed_out": False}


class SandboxRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = build_test_stores()
        self.sandboxes: _RecordingSandboxes | None = _RecordingSandboxes()
        pool = SimpleNamespace(
            list_registered_agents=lambda: ["research", "writer"],
            get_runtime_store=lambda: self.store,
            get_sandboxes=lambda: self.sandboxes,
        )
        app = FastAPI()
        app.include_router(build_sandbox_router())
        app.state.runtime_state = SimpleNamespace(pool=pool, host_secret=SECRET)
        self.http = TestClient(app)
        self._accept("req-1", agent_id="research", user_id="u-1")

    def _accept(self, request_id: str, *, agent_id: str, user_id: str | None) -> None:
        request_metadata = {"user_id": user_id} if user_id else {}
        asyncio.run(
            self.store.append_event(
                RuntimeEvent(
                    request_id=request_id,
                    app_id=agent_id,
                    agent_id=agent_id,
                    session_id="s-1",
                    event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                    dedupe_key="request.accepted",
                    lifecycle=RequestStatus.RUNNING,
                    payload={"request_metadata": request_metadata},
                )
            )
        )

    def _exec(self, *, caller: str = "research", **overrides: Any):
        body = {"request_id": "req-1", "command": "ls", "timeout": 5, **overrides}
        return self.http.post(
            "/sandbox/exec",
            json=body,
            headers={"Authorization": f"Bearer {derive_agent_token(SECRET, caller)}"},
        )

    def test_runs_in_the_request_users_sandbox_in_the_callers_folder(self) -> None:
        resp = self._exec()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"output": "ok", "exit_code": 0, "timed_out": False})
        self.assertEqual(
            self.sandboxes.calls,
            [{"user_id": "u-1", "agent_id": "research", "command": "ls", "timeout": 5.0}],
        )

    def test_invalid_token_is_401(self) -> None:
        resp = self.http.post(
            "/sandbox/exec",
            json={"request_id": "req-1", "command": "ls"},
            headers={"Authorization": "Bearer forged"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_unknown_request_is_404(self) -> None:
        self.assertEqual(self._exec(request_id="missing").status_code, 404)

    def test_another_agents_request_is_403(self) -> None:
        resp = self._exec(caller="writer")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.sandboxes.calls, [])

    def test_a_request_without_a_user_is_403(self) -> None:
        self._accept("req-old", agent_id="research", user_id=None)
        self.assertEqual(self._exec(request_id="req-old").status_code, 403)

    def test_bad_bodies_are_400(self) -> None:
        for overrides in ({"command": ""}, {"request_id": ""}, {"timeout": -1}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self._exec(**overrides).status_code, 400)

    def test_no_sandboxes_and_capacity_are_503(self) -> None:
        self.sandboxes.error = SandboxCapacityError("all 20 sandboxes are busy")
        resp = self._exec()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "SANDBOX_CAPACITY")

        self.sandboxes = None
        resp = self._exec()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "SANDBOX_UNAVAILABLE")


class SandboxBashEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_agents_bash_call_runs_in_the_request_users_sandbox(self) -> None:
        command = "echo hello > notes.txt && cat notes.txt && pwd"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ):
            pool = (
                HostBuilder()
                .agent(
                    build_tool_calling_spec(
                        agent_id="research", tool_name="bash", arguments={"command": command}
                    ),
                    metadata=metadata(),
                )
                .build()
            )
            provider = LocalSandboxProvider()
            pool.configure_runtime_database_url("postgresql://test/runtime")
            pool.configure_sandboxes(SandboxManager(provider))
            await pool.start()
            try:
                client = pool.get_client("research")
                request_id = await client.post_request("go", session_id="s-1", user_id="u-1")
                events = [event async for event in client.stream_response(request_id, timeout=20)]
                folder = provider.workspaces("u-1") / "research"
                self.assertEqual((folder / "notes.txt").read_text(), "hello\n")
                self.assertFalse(provider.workspaces("u-2").exists())
            finally:
                await pool.close()

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(events[-1]["data"]["response"]["text"], f"hello\n{folder}")


if __name__ == "__main__":
    unittest.main()

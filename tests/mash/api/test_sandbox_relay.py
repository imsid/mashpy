"""The host's sandbox relay and links, and the sandbox tools end to end."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient

from conftest import build_test_stores
from mash.api.routes.sandbox import LINK_CSP, build_link_router, build_sandbox_router
from mash.runtime import HostBuilder
from mash.runtime.events import RequestStatus, RuntimeEvent, RuntimeEventType
from mash.runtime.host.tokens import derive_agent_token
from mash.sandbox.manager import SandboxCapacityError, SandboxManager
from mash.sandbox.tokens import derive_link_token
from mash.testing.runtime_fixtures import build_tool_calling_spec, metadata
from mash.testing.sandboxes import LocalSandboxProvider

SECRET = "host-secret"
PUBLIC = "http://mash.example"


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
        app.state.runtime_state = SimpleNamespace(
            pool=pool, host_secret=SECRET, public_url=PUBLIC
        )
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
        link = f"{PUBLIC}/s/{derive_link_token(SECRET, 'u-1')}"
        self.assertEqual(
            self.sandboxes.calls,
            [
                {
                    "user_id": "u-1",
                    "agent_id": "research",
                    "command": "ls",
                    "timeout": 5.0,
                    "stdin": None,
                    "env": {
                        "MASH_FILES_URL": f"{link}/files/research",
                        "MASH_PORTS_URL": f"{link}/ports",
                    },
                }
            ],
        )

    def test_stdin_is_passed_through(self) -> None:
        self.assertEqual(self._exec(stdin="print(1)").status_code, 200)
        self.assertEqual(self.sandboxes.calls[0]["stdin"], "print(1)")
        self.assertEqual(self._exec(stdin=5).status_code, 400)

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


class SandboxFilesAndLinksTests(unittest.IsolatedAsyncioTestCase):
    """Relay file calls and user links over a real manager and local sandboxes."""

    async def asyncSetUp(self) -> None:
        self.store, _ = build_test_stores()
        self.provider = LocalSandboxProvider()
        self.manager = SandboxManager(self.provider)
        pool = SimpleNamespace(
            list_registered_agents=lambda: ["research", "writer"],
            get_runtime_store=lambda: self.store,
            get_sandboxes=lambda: self.manager,
        )
        app = FastAPI()
        app.include_router(build_sandbox_router())
        app.include_router(build_link_router())
        app.state.runtime_state = SimpleNamespace(
            pool=pool, host_secret=SECRET, public_url=PUBLIC
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        )
        await self.store.append_event(
            RuntimeEvent(
                request_id="req-1",
                app_id="research",
                agent_id="research",
                session_id="s-1",
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key="request.accepted",
                lifecycle=RequestStatus.RUNNING,
                payload={"request_metadata": {"user_id": "u-1"}},
            )
        )
        self.auth = {"Authorization": f"Bearer {derive_agent_token(SECRET, 'research')}"}
        self.link = f"/s/{derive_link_token(SECRET, 'u-1')}"

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await self.manager.close()

    async def test_files_go_to_the_callers_folder_of_the_request_user(self) -> None:
        put = await self.http.put(
            "/sandbox/files",
            params={"request_id": "req-1", "path": "data/sales.csv"},
            content=b"region,total\nwest,3\n",
            headers=self.auth,
        )
        self.assertEqual(put.status_code, 200, put.text)
        folder = self.provider.workspaces("u-1") / "research"
        self.assertEqual((folder / "data" / "sales.csv").read_bytes(), b"region,total\nwest,3\n")

        got = await self.http.get(
            "/sandbox/files",
            params={"request_id": "req-1", "path": "data/sales.csv"},
            headers=self.auth,
        )
        self.assertEqual(got.content, b"region,total\nwest,3\n")

        missing = await self.http.get(
            "/sandbox/files", params={"request_id": "req-1", "path": "nope"}, headers=self.auth
        )
        self.assertEqual(missing.status_code, 404)
        escape = await self.http.get(
            "/sandbox/files", params={"request_id": "req-1", "path": "../writer/x"},
            headers=self.auth,
        )
        self.assertEqual(escape.status_code, 400)
        writer = await self.http.get(
            "/sandbox/files",
            params={"request_id": "req-1", "path": "data/sales.csv"},
            headers={"Authorization": f"Bearer {derive_agent_token(SECRET, 'writer')}"},
        )
        self.assertEqual(writer.status_code, 403)

    async def test_a_file_link_serves_the_file_with_its_type_in_a_sandboxed_page(self) -> None:
        folder = self.provider.workspaces("u-1") / "research"
        await self.manager.exec(
            "u-1",
            agent_id="research",
            command="mkdir -p site && echo '<h1>Sales</h1>' > report.html && cp report.html site/index.html",
            timeout=10,
        )

        page = await self.http.get(f"{self.link}/files/research/report.html")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text, "<h1>Sales</h1>\n")
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        self.assertEqual(page.headers["content-security-policy"], LINK_CSP)

        index = await self.http.get(f"{self.link}/files/research/site/")
        self.assertEqual(index.text, "<h1>Sales</h1>\n")
        self.assertTrue((folder / "report.html").exists())

    async def test_links_need_a_token_the_host_made(self) -> None:
        for link in ("/s/forged/files/research/report.html", "/s/dS0x.0000/files/research/x"):
            with self.subTest(link=link):
                self.assertEqual((await self.http.get(link)).status_code, 404)
        self.assertEqual(self.provider.ensure_calls, [])

    async def test_a_port_link_forwards_to_a_server_in_the_sandbox(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        await self.manager.exec(
            "u-1",
            agent_id="research",
            command=(
                "mkdir -p app && echo '<p>live</p>' > app/index.html && "
                f"nohup python3 -m http.server {port} --bind 127.0.0.1 -d app >app.log 2>&1 &"
            ),
            timeout=10,
        )
        try:
            redirect = await self.http.get(f"{self.link}/ports/{port}")
            self.assertEqual(redirect.status_code, 307)
            self.assertEqual(redirect.headers["location"], f"{port}/")
            for _ in range(100):
                page = await self.http.get(f"{self.link}/ports/{port}/")
                if page.status_code == 200:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(page.text, "<p>live</p>\n")
            self.assertEqual(page.headers["content-security-policy"], LINK_CSP)
        finally:
            await self.manager.exec(
                "u-1", agent_id="research", command=f"pkill -f 'http.server {port}' || true",
                timeout=10,
            )


class SandboxToolsEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one_call(self, tool_name: str, arguments: dict[str, Any]):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ):
            pool = (
                HostBuilder()
                .agent(
                    build_tool_calling_spec(
                        agent_id="research", tool_name=tool_name, arguments=arguments
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
                files = {
                    str(path.relative_to(folder)): path.read_bytes()
                    for path in folder.rglob("*")
                    if path.is_file()
                } if folder.exists() else {}
                other_user = provider.workspaces("u-2").exists()
            finally:
                await pool.close()
        self.assertFalse(other_user)
        self.assertEqual(events[-1]["event"], "request.completed")
        return events[-1]["data"]["response"]["text"], files, folder

    async def test_run_code_runs_python_in_the_request_users_sandbox(self) -> None:
        code = (
            "import os, sqlite3\n"
            "db = sqlite3.connect('data.db')\n"
            "db.execute('create table t (n int)')\n"
            "db.executemany('insert into t values (?)', [(1,), (2,)])\n"
            "db.commit()\n"
            "print(\"it's\", db.execute('select sum(n) from t').fetchone()[0], os.getcwd())\n"
        )
        text, files, folder = await self._run_one_call(
            "run_code", {"language": "python", "code": code}
        )

        self.assertEqual(text, f"it's 3 {os.path.realpath(folder)}")
        self.assertEqual(sorted(files), ["data.db"])

    async def test_run_code_prints_the_users_file_link(self) -> None:
        text, _, _ = await self._run_one_call(
            "run_code", {"language": "shell", "code": 'echo "$MASH_FILES_URL/report.html"'}
        )

        self.assertRegex(text, r"^http://127\.0\.0\.1:\d+/s/[^/]+/files/research/report\.html$")

    async def test_sandbox_put_copies_a_local_file_into_the_folder(self) -> None:
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
            fh.write(b"a,b\n1,2\n")
        self.addCleanup(os.unlink, fh.name)

        text, files, _ = await self._run_one_call(
            "sandbox_put", {"src": fh.name, "dest": "in/data.csv"}
        )

        self.assertEqual(text, "Copied 8 bytes to in/data.csv in your sandbox folder.")
        self.assertEqual(files, {"in/data.csv": b"a,b\n1,2\n"})


if __name__ == "__main__":
    unittest.main()

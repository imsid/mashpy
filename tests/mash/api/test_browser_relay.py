"""The host's browser relay, live-view links and site API, over local browsers."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import uvicorn
import websockets
from fastapi import FastAPI
from starlette.testclient import TestClient

from conftest import build_test_stores
from mash.api.routes.browser import (
    build_browser_api_router,
    build_browser_link_router,
    build_browser_router,
)
from mash.api.routes.common import APIError, error_payload
from mash.browser.manager import BrowserManager
from mash.browser.tokens import make_view_token
from mash.runtime.events import RequestStatus, RuntimeEvent, RuntimeEventType
from mash.runtime.host.tokens import derive_agent_token
from mash.sandbox.manager import SandboxManager
from mash.testing.browsers import FakePage, LocalBrowserProvider
from mash.testing.sandboxes import LocalSandboxProvider

SECRET = "host-secret"
PUBLIC = "http://mash.example"
PAGES = {
    "https://www.pge.com/": FakePage(
        title="PG&E",
        refs={
            "pdf": ("download", "bill.pdf", b"%PDF-1.7 bill"),
            "attach": ("chooser",),
        },
    ),
}


class _Spec:
    def __init__(self, sites: list[str]) -> None:
        self._sites = sites

    def browser_sites(self) -> list[str]:
        return self._sites


class _VNC:
    """A TCP server that echoes bytes, standing in for x11vnc."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            while data := conn.recv(4096):
                conn.sendall(data)


class BrowserRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, _ = build_test_stores()
        self._novnc = tempfile.TemporaryDirectory()
        Path(self._novnc.name, "vnc.html").write_text("<html>noVNC</html>")
        self.vnc = _VNC()
        self.provider = LocalBrowserProvider(
            host_secret=SECRET, pages=PAGES, novnc_dir=Path(self._novnc.name), vnc_port=self.vnc.port
        )
        self.sandbox_provider = LocalSandboxProvider(host_secret=SECRET)
        self.browsers = BrowserManager(self.provider, host_secret=SECRET)
        self.sandboxes = SandboxManager(self.sandbox_provider)
        specs = {"billing": _Spec(["https://*.pge.com"]), "writer": _Spec([])}
        pool = SimpleNamespace(
            list_registered_agents=lambda: list(specs),
            get_runtime_store=lambda: self.store,
            get_sandboxes=lambda: self.sandboxes,
            get_browsers=lambda: self.browsers,
            get_registered_agent_spec=specs.get,
        )
        app = FastAPI()

        @app.exception_handler(APIError)
        async def _api_error(_, exc: APIError):
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=exc.status_code, content=error_payload(exc.code, exc.message))

        app.include_router(build_browser_router())
        app.include_router(build_browser_link_router())
        app.include_router(build_browser_api_router(), prefix="/api/v1")
        app.state.runtime_state = SimpleNamespace(pool=pool, host_secret=SECRET, public_url=PUBLIC)
        self.http = TestClient(app)
        self.http.__enter__()
        self._accept("req-1", agent_id="billing", user_id="u-1")

    def tearDown(self) -> None:
        self.http.portal.call(self.provider.close)
        self.http.portal.call(self.sandbox_provider.close)
        self.http.__exit__(None, None, None)
        self.vnc.sock.close()
        self._novnc.cleanup()

    def _accept(self, request_id: str, *, agent_id: str, user_id: str) -> None:
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
                    payload={"request_metadata": {"user_id": user_id}},
                )
            )
        )

    def post(self, route: str, *, caller: str = "billing", **body: Any):
        return self.http.post(
            f"/browser{route}",
            json={"request_id": "req-1", **body},
            headers={"Authorization": f"Bearer {derive_agent_token(SECRET, caller)}"},
        )

    def navigate(self) -> None:
        resp = self.post("/call", tool="browser_navigate", arguments={"url": "https://www.pge.com/"})
        self.assertFalse(resp.json()["is_error"], resp.json())

    def test_calls_run_in_the_request_users_browser_on_the_spec_sites(self) -> None:
        self.navigate()
        off = self.post("/call", tool="browser_navigate", arguments={"url": "https://evil.io/"})

        self.assertIn("u-1", self.provider.browsers)
        self.assertTrue(off.json()["is_error"])
        self.assertIn("https://*.pge.com", off.json()["content"])

    def test_tools_outside_the_relayed_set_are_refused(self) -> None:
        resp = self.post("/call", tool="browser_evaluate", arguments={"function": "() => 1"})
        self.assertEqual(resp.status_code, 400)

    def test_an_agent_without_sites_or_another_agents_request_is_refused(self) -> None:
        self.assertEqual(self.post("/call", caller="writer", tool="browser_snapshot").status_code, 403)
        self._accept("req-2", agent_id="writer", user_id="u-1")
        resp = self.http.post(
            "/browser/call",
            json={"request_id": "req-2", "tool": "browser_snapshot"},
            headers={"Authorization": f"Bearer {derive_agent_token(SECRET, 'billing')}"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_save_copies_a_download_into_the_callers_sandbox_folder(self) -> None:
        self.navigate()
        clicked = self.post("/call", tool="browser_click", arguments={"target": "pdf"})
        self.assertIn("download_id: dl-1", clicked.json()["content"])

        saved = self.post("/save", download_id="dl-1", path="bills/2026-10.pdf")

        self.assertFalse(saved.json()["is_error"], saved.json())
        written = self.sandbox_provider.workspaces("u-1") / "billing" / "bills" / "2026-10.pdf"
        self.assertEqual(written.read_bytes(), b"%PDF-1.7 bill")
        self.assertTrue(self.post("/save", download_id="dl-1", path="../x").json()["is_error"])
        self.assertTrue(self.post("/save", download_id="nope", path="x").json()["is_error"])

    def test_upload_gives_a_sandbox_file_to_the_file_chooser(self) -> None:
        folder = self.sandbox_provider.workspaces("u-1") / "billing"
        folder.mkdir(parents=True)
        (folder / "meter.csv").write_bytes(b"kwh\n9\n")
        self.navigate()

        resp = self.post("/upload", path="meter.csv", target="attach", element="Attach")

        self.assertFalse(resp.json()["is_error"], resp.json())
        [[path]] = self.provider.browsers["u-1"].uploaded
        self.assertEqual(Path(path).read_bytes(), b"kwh\n9\n")

    def test_handoff_link_opens_the_live_view_until_the_handoff_ends(self) -> None:
        self.navigate()
        started = self.post("/handoff").json()
        self.assertTrue(started["url"].startswith(f"{PUBLIC}/s/"))
        self.assertEqual(started["timeout_seconds"], 900)
        path = urlsplit(started["url"]).path

        root = self.http.get(path, follow_redirects=False)
        self.assertEqual(root.status_code, 307)
        location = urlsplit(root.headers["location"])
        self.assertEqual(location.path, "vnc.html")
        socket_path = parse_qs(location.query)["path"][0]
        self.assertEqual(f"/{socket_path}", f"{path}websockify")
        self.assertEqual(self.http.get(f"{path}vnc.html").text, "<html>noVNC</html>")

        self.assertEqual(self.http.portal.call(self._through_view, f"{path}websockify"), b"RFB 003.008\n")

        ended = self.post("/handoff/end").json()
        self.assertIn("PG&E", ended["content"])
        self.assertEqual(self.http.get(f"{path}vnc.html").status_code, 404)

    async def _through_view(self, path: str) -> bytes:
        """Bytes sent through the live view's websocket, echoed by the VNC stand-in.

        A real server, since the test client cancels a websocket handler as it
        disconnects.
        """
        server = uvicorn.Server(
            uvicorn.Config(self.http.app, host="127.0.0.1", port=0, log_level="warning")
        )
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{port}{path}", subprotocols=["binary"]
            ) as ws:
                await ws.send(b"RFB 003.008\n")
                return await ws.recv()
        finally:
            server.should_exit = True
            await task

    def test_a_request_token_without_an_open_handoff_is_refused(self) -> None:
        token = make_view_token(SECRET, "u-1", "req-9", 2**40)
        self.assertEqual(self.http.get(f"/s/{token}/browser/vnc.html").status_code, 404)

    def test_an_operator_token_works_until_it_expires(self) -> None:
        token = make_view_token(SECRET, "u-1", "", 2**40)
        self.assertEqual(self.http.get(f"/s/{token}/browser/vnc.html").status_code, 200)
        expired = make_view_token(SECRET, "u-1", "", 1)
        self.assertEqual(self.http.get(f"/s/{expired}/browser/vnc.html").status_code, 404)

    def test_apps_list_and_clear_signed_in_sites_and_delete_the_profile(self) -> None:
        self.navigate()
        self.provider.browsers["u-1"].cdp.cookies = {"pge.com": 4}

        listed = self.http.get("/api/v1/browser/users/u-1/sites").json()
        self.assertEqual(listed["data"]["sites"], [{"domain": "pge.com", "cookies": 4}])
        cleared = self.http.delete("/api/v1/browser/users/u-1/sites/pge.com").json()
        self.assertEqual(cleared["data"]["cookies"], 4)
        deleted = self.http.delete("/api/v1/browser/users/u-1").json()
        self.assertTrue(deleted["data"]["deleted"])
        self.assertEqual(self.provider.deleted, ["u-1"])


if __name__ == "__main__":
    unittest.main()

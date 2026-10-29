"""The daemon against a real Chromium and Playwright MCP, on this machine.

Opt in with ``MASH_BROWSER_LIVE=1``. Needs Chrome or Chromium (``MASH_BROWSER_CHROMIUM``,
or Google Chrome on macOS), Node with npx, and network access to fetch the
pinned ``@playwright/mcp``. Runs headless; the container runs Chromium headed.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
import uvicorn

from mash.browser.catalog import PLAYWRIGHT_MCP_VERSION
from mash.browser.cdp import ChromiumCDP
from mash.browser.daemon import BrowserDaemon, PlaywrightMCPSession
from mash.workflows.spec import StepContext

LIVE = os.environ.get("MASH_BROWSER_LIVE") == "1"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TOKEN = "live-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

SITE = {
    "index.html": (
        "<title>Bills</title><h1>Your bills</h1>"
        '<a href="bill.pdf" download>Download October bill</a> '
        '<a href="http://localhost:{port}/away.html">Leave</a> '
        '<label>Meter file <input type="file" id="meter"></label>'
    ),
    "bill.pdf": "%PDF-1.7 october",
    "away.html": "<title>Away</title>",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _chromium() -> str | None:
    explicit = os.environ.get("MASH_BROWSER_CHROMIUM")
    if explicit:
        return explicit
    return MAC_CHROME if os.path.exists(MAC_CHROME) else shutil.which("chromium")


@unittest.skipUnless(LIVE and _chromium() and shutil.which("npx"), "set MASH_BROWSER_LIVE=1")
class LiveBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        site = home / "site"
        site.mkdir()
        self.site_port = _free_port()
        for name, body in SITE.items():
            (site / name).write_text(body.replace("{port}", str(self.site_port)))
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)  # noqa: E731
        self.site = http.server.ThreadingHTTPServer(("127.0.0.1", self.site_port), handler)
        threading.Thread(target=self.site.serve_forever, daemon=True).start()

        cdp_port, mcp_port = _free_port(), _free_port()
        self.children = [
            subprocess.Popen(
                [_chromium(), "--headless=new", f"--remote-debugging-port={cdp_port}",
                 f"--user-data-dir={home / 'profile'}", "--no-first-run",
                 "--no-default-browser-check", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        ]
        await self._wait(f"http://127.0.0.1:{cdp_port}/json/version")
        self.children.append(
            subprocess.Popen(
                ["npx", "-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
                 "--cdp-endpoint", f"http://127.0.0.1:{cdp_port}", "--port", str(mcp_port),
                 "--caps=vision", "--output-dir", str(home / "output")],
                cwd=home, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        )
        await self._wait(f"http://localhost:{mcp_port}/mcp", any_status=True)

        async def connect() -> PlaywrightMCPSession:
            return await PlaywrightMCPSession.open(f"http://localhost:{mcp_port}/mcp")

        daemon = BrowserDaemon(
            token=TOKEN,
            connect=connect,
            cdp=ChromiumCDP(f"http://127.0.0.1:{cdp_port}", downloads_dir=home / "downloads"),
            home=home,
        )
        self.home = home
        self.server = uvicorn.Server(
            uvicorn.Config(daemon.app, host="127.0.0.1", port=0, log_level="warning")
        )
        self.server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.http = httpx.AsyncClient(base_url=self.base, headers=AUTH, timeout=120)
        self.sites = [f"http://127.0.0.1:{self.site_port}"]

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self.server.should_exit = True
        await self.serving
        for child in reversed(self.children):
            child.terminate()
            with contextlib.suppress(Exception):
                child.wait(timeout=10)
        self.site.shutdown()
        self.tmp.cleanup()

    async def _wait(self, url: str, *, any_status: bool = False) -> None:
        deadline = time.monotonic() + 90
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                with contextlib.suppress(httpx.HTTPError):
                    response = await client.get(url)
                    if any_status or response.status_code == 200:
                        return
                await asyncio.sleep(0.3)
        raise RuntimeError(f"{url} did not come up")

    async def call(self, request_id: str, tool: str, **arguments):
        response = await self.http.post(
            f"/requests/{request_id}/call",
            json={"tool": tool, "arguments": arguments, "sites": self.sites},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_a_request_browses_downloads_uploads_and_stays_on_its_sites(self) -> None:
        page = await self.call("r-1", "browser_navigate", url=f"{self.sites[0]}/index.html")
        self.assertFalse(page["is_error"], page["content"])
        self.assertIn("Download October bill", page["content"])
        self.assertIn("```yaml", page["content"])

        ref = _ref(page["content"], "Download October bill")
        clicked = await self.call("r-1", "browser_click", target=ref, element="Download")
        self.assertIn("file: bill.pdf, state: completed", clicked["content"], clicked["content"])
        download_id = clicked["content"].split("download_id: ")[1].split(",")[0]
        download = await self.http.get(f"/downloads/{download_id}")
        self.assertEqual(download.content, b"%PDF-1.7 october")

        meter = _ref(page["content"], "Meter file")
        uploaded = await self.http.post(
            "/requests/r-1/upload",
            params={"name": "meter.csv", "target": meter, "element": "Meter file",
                    "sites": json.dumps(self.sites)},
            content=b"kwh\n9\n",
        )
        self.assertFalse(uploaded.json()["is_error"], uploaded.json()["content"])

        left = await self.call("r-1", "browser_click", target=_ref(page["content"], "Leave"), element="Leave")
        self.assertTrue(left["is_error"])
        self.assertIn("outside the sites", left["content"])
        back = await self.call("r-1", "browser_snapshot")
        self.assertEqual(back["page_url"], f"{self.sites[0]}/index.html")

        shot = await self.call("r-1", "browser_take_screenshot")
        self.assertTrue(shot["images"])

    async def test_two_requests_keep_their_own_tabs(self) -> None:
        await self.call("r-1", "browser_navigate", url=f"{self.sites[0]}/index.html")
        await self.call("r-2", "browser_navigate", url=f"{self.sites[0]}/bill.pdf")

        first = await self.call("r-1", "browser_snapshot")
        self.assertEqual(first["page_url"], f"{self.sites[0]}/index.html")
        self.assertEqual((await self.http.delete("/requests/r-1")).json(), {"closed": True})

    async def test_workflow_code_drives_the_same_profile_over_cdp(self) -> None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")

        class _Browsers:
            @contextlib.asynccontextmanager
            async def hold(inner, user_id):
                yield type("E", (), {"base_url": self.base, "token": TOKEN})()

        ctx = StepContext(run_id="r", step_id="s", workflow_input={}, user_id="u-1", browsers=_Browsers())
        async with ctx.browser() as context:
            page = await context.new_page()
            await page.goto(f"{self.sites[0]}/index.html")
            title = await page.title()
        self.assertEqual(title, "Bills")


def _ref(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        if label in line and "[ref=" in line:
            return line.split("[ref=")[1].split("]")[0]
    raise AssertionError(f"no ref for {label!r} in:\n{snapshot}")


if __name__ == "__main__":
    unittest.main()

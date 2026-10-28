"""Browsers on localhost ports with a fake Chromium, for tests.

``FakeBrowser`` stands in for Chromium and Playwright MCP together. It keeps
what the spike showed matters: every MCP session sees the same tabs, each
session has its own current tab, action results name a snapshot file under
``output/`` while ``browser_snapshot`` returns the tree inline, and downloads
arrive through CDP. Pages are a dict of URL to ``FakePage``.

``LocalBrowserProvider`` runs each user's ``BrowserDaemon`` in this process on
its own ``127.0.0.1`` port, driving a ``FakeBrowser`` per user.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from mash.browser.cdp import Download
from mash.browser.daemon import BrowserDaemon
from mash.browser.provider import BrowserEndpoint
from mash.browser.tokens import derive_browser_token
from mash.sandbox.provider import SandboxInfo, sandbox_key

DEFAULT_LOCAL_SECRET = "local-browsers"
# A 1x1 PNG.
PIXEL_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()


@dataclass
class FakePage:
    """A page: its title, and what clicking each ref does."""

    title: str
    # ref -> ("goto", url) | ("download", file name, bytes) | ("chooser",)
    refs: dict[str, tuple[Any, ...]] = field(default_factory=dict)


@dataclass
class _Tab:
    history: list[str] = field(default_factory=lambda: ["about:blank"])

    @property
    def url(self) -> str:
        return self.history[-1]


class FakeCDP:
    """``ChromiumCDP``'s surface, over the fake browser's downloads and cookies."""

    def __init__(self, downloads_dir: Path) -> None:
        self.downloads_dir = downloads_dir
        self.downloads: dict[str, Download] = {}
        self.cookies: dict[str, int] = {}

    async def start(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

    async def browser_ws_url(self) -> str:
        raise RuntimeError("the fake browser has no CDP websocket")

    def downloads_since(self, started: float) -> list[Download]:
        return [d for d in self.downloads.values() if d.started_at >= started]

    def download_path(self, download_id: str) -> Path | None:
        download = self.downloads.get(download_id)
        if download is None or download.state != "completed":
            return None
        return self.downloads_dir / download.id

    async def cookie_domains(self) -> list[dict[str, Any]]:
        return [{"domain": d, "cookies": n} for d, n in sorted(self.cookies.items())]

    async def clear_domain(self, domain: str) -> int:
        return self.cookies.pop(domain, 0)


class FakeBrowser:
    """Chromium with Playwright MCP attached, in memory."""

    def __init__(self, home: Path, pages: dict[str, FakePage] | None = None) -> None:
        self.home = home
        self.pages = dict(pages or {})
        self.tabs: list[_Tab] = [_Tab()]
        self.cdp = FakeCDP(home / "downloads")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploaded: list[list[str]] = []
        self._chooser_open = False
        self._snapshots = 0
        (home / "output").mkdir(parents=True, exist_ok=True)

    async def connect(self) -> "FakeSession":
        return FakeSession(self)

    def _page(self, url: str) -> FakePage:
        return self.pages.get(url) or FakePage(title="")

    def snapshot_yaml(self, tab: _Tab) -> str:
        page = self._page(tab.url)
        lines = [f"- generic [ref=e1]: {page.title}"]
        lines += [f"  - link \"{ref}\" [ref={ref}]" for ref in page.refs]
        return "\n".join(lines)

    def tab_list(self, current: int | None) -> str:
        return "\n".join(
            f"- {i}: {'(current) ' if i == current else ''}[{self._page(t.url).title}]({t.url})"
            for i, t in enumerate(self.tabs)
        )


class FakeSession:
    """One MCP session: its own current tab over the shared ones."""

    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        # Like Playwright MCP attached over CDP: the first existing tab.
        self.current: _Tab | None = browser.tabs[0] if browser.tabs else None
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        b = self.browser
        b.calls.append((name, dict(arguments)))
        if name == "browser_tabs":
            return self._tabs(arguments)
        if self.current is None:
            return _error("No open tabs")
        tab = self.current
        if name == "browser_navigate":
            tab.history.append(str(arguments["url"]))
            return self._page_result(tab, inline=False)
        if name == "browser_navigate_back":
            if len(tab.history) > 1:
                tab.history.pop()
            return self._page_result(tab, inline=False)
        if name == "browser_snapshot":
            return self._page_result(tab, inline=True)
        if name == "browser_click":
            action = b._page(tab.url).refs.get(str(arguments.get("target")))
            if action is None:
                return _error(f"Ref {arguments.get('target')} not found")
            if action[0] == "goto":
                tab.history.append(action[1])
            elif action[0] == "download":
                guid = f"dl-{len(b.cdp.downloads) + 1}"
                (b.cdp.downloads_dir / guid).write_bytes(action[2])
                b.cdp.downloads[guid] = Download(
                    id=guid, file_name=action[1], url=tab.url,
                    started_at=time.monotonic(), state="completed",
                )
            elif action[0] == "chooser":
                b._chooser_open = True
            return self._page_result(tab, inline=False)
        if name == "browser_file_upload":
            if not b._chooser_open:
                return _error("No file chooser is open")
            b._chooser_open = False
            b.uploaded.append(list(arguments.get("paths") or []))
            return self._page_result(tab, inline=False)
        if name == "browser_take_screenshot":
            result = self._page_result(tab, inline=False)
            result["content"].append({"type": "image", "data": PIXEL_PNG, "mimeType": "image/png"})
            return result
        return self._page_result(tab, inline=False)

    def _tabs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        b = self.browser
        action = arguments.get("action")
        if action == "new":
            tab = _Tab()
            if arguments.get("url"):
                tab.history.append(str(arguments["url"]))
            b.tabs.append(tab)
            self.current = tab
        elif action == "close":
            if self.current in b.tabs:
                b.tabs.remove(self.current)
            self.current = b.tabs[-1] if b.tabs else None
        elif action == "select":
            self.current = b.tabs[int(arguments["index"])]
        index = b.tabs.index(self.current) if self.current in b.tabs else None
        return {"content": [{"type": "text", "text": "### Result\n" + b.tab_list(index)}], "isError": False}

    def _page_result(self, tab: _Tab, *, inline: bool) -> dict[str, Any]:
        b = self.browser
        page = b._page(tab.url)
        if inline:
            snapshot = "```yaml\n" + b.snapshot_yaml(tab) + "\n```"
        else:
            b._snapshots += 1
            path = f"output/page-{b._snapshots}.yml"
            (b.home / path).write_text(b.snapshot_yaml(tab))
            snapshot = f"- [Snapshot]({path})"
        text = (
            "### Ran Playwright code\n```js\nawait page.doSomething();\n```\n"
            f"### Page\n- Page URL: {tab.url}\n- Page Title: {page.title}\n"
            f"### Snapshot\n{snapshot}\n"
        )
        return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"### Error\n{message}"}], "isError": True}


class _Server(uvicorn.Server):
    # Many servers share this process; none of them should take over signals.
    def capture_signals(self):  # type: ignore[override]
        return contextlib.nullcontext()


class LocalBrowserProvider:
    """Each user's browser daemon on a localhost port in this process."""

    def __init__(
        self,
        *,
        host_secret: str = DEFAULT_LOCAL_SECRET,
        pages: dict[str, FakePage] | None = None,
        novnc_dir: Path | None = None,
        vnc_port: int = 5900,
    ) -> None:
        self._host_secret = host_secret
        self._pages = dict(pages or {})
        self._novnc_dir = novnc_dir
        self._vnc_port = vnc_port
        self.root = Path(tempfile.mkdtemp(prefix="mash-browsers-"))
        self.browsers: dict[str, FakeBrowser] = {}
        self._running: dict[str, tuple[_Server, asyncio.Task[None], BrowserEndpoint]] = {}
        self.deleted: list[str] = []

    def browser(self, user_id: str) -> FakeBrowser:
        if user_id not in self.browsers:
            home = self.root / sandbox_key(user_id)
            self.browsers[user_id] = FakeBrowser(home, self._pages)
        return self.browsers[user_id]

    async def ensure(self, user_id: str) -> BrowserEndpoint:
        running = self._running.get(user_id)
        if running is not None:
            return running[2]
        fake = self.browser(user_id)
        token = derive_browser_token(self._host_secret, user_id)
        daemon = BrowserDaemon(
            token=token, connect=fake.connect, cdp=fake.cdp, home=fake.home,
            download_wait_seconds=0.0, novnc_dir=self._novnc_dir, vnc_port=self._vnc_port,
        )
        server = _Server(uvicorn.Config(daemon.app, host="127.0.0.1", port=0, log_level="warning"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("local browser server exited during startup")
            await asyncio.sleep(0.01)
        port = int(server.servers[0].sockets[0].getsockname()[1])
        endpoint = BrowserEndpoint(user_id=user_id, base_url=f"http://127.0.0.1:{port}", token=token)
        self._running[user_id] = (server, task, endpoint)
        return endpoint

    async def stop(self, user_id: str) -> None:
        running = self._running.pop(user_id, None)
        if running is None:
            return
        server, task, _ = running
        server.should_exit = True
        await task

    async def delete(self, user_id: str) -> None:
        await self.stop(user_id)
        self.browsers.pop(user_id, None)
        self.deleted.append(user_id)

    async def list(self) -> list[SandboxInfo]:
        return [
            SandboxInfo(user_id=u, name=f"local-browser-{sandbox_key(u)}", running=True, status="running")
            for u in self._running
        ]

    async def close(self) -> None:
        for user_id in list(self._running):
            await self.stop(user_id)
        shutil.rmtree(self.root, ignore_errors=True)


__all__ = ["FakeBrowser", "FakeCDP", "FakePage", "LocalBrowserProvider", "PIXEL_PNG"]

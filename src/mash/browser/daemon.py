"""The browser daemon: drives one user's Chromium for the host.

``mash browser serve`` runs this app in each user's browser container, next to
Chromium and Playwright MCP (``processes.py``). The daemon is Playwright MCP's
client. It holds one MCP session per request, each working in a tab of its
own, and one CDP connection of its own (``cdp.py``) for downloads and cookies.

- ``POST /requests/<request_id>/call`` runs one relayed tool and returns
  ``{content, images, page_url, is_error}``. The first call of a request opens
  its session and a new tab.
- ``POST /requests/<request_id>/upload`` writes the body to the request's
  uploads folder and gives it to the file chooser that clicking ``target``
  opens.
- ``POST /requests/<request_id>/front`` brings the request's tab to the front,
  for the user's live view.
- ``DELETE /requests/<request_id>`` closes the request's tab and session.
- ``GET /downloads/<id>`` returns a finished download.
- ``GET /sites`` and ``DELETE /sites/<domain>`` list and clear cookies.
- ``/view/`` serves noVNC's client; ``/view/websockify`` bridges its websocket
  to the VNC server on the display Chromium draws to.
- ``/cdp`` bridges a websocket to Chromium's CDP, for workflow code.

Every call carries the calling agent's sites (``sites.py``). A navigation to a
URL outside them fails before it runs, and a call that leaves them, by a
click or a redirect, goes back and returns an error instead of the page.

Every route but ``/health`` requires ``Authorization: Bearer <token>``, the
user's browser token (``mash.browser.tokens``). The host is the only caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .catalog import RELAYED_TOOLS
from .cdp import ChromiumCDP
from .sites import check_sites, origin_of, url_allowed

MASH_BROWSER_TOKEN_ENV = "MASH_BROWSER_TOKEN"
BROWSER_PORT = 9000
BROWSER_HOME = "/home/browser"
# A request's tab and session close after this long without a call.
SESSION_IDLE_SECONDS = 600.0
# How long a call waits for a download it started to finish.
DOWNLOAD_WAIT_SECONDS = 15.0
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
WEB_CONTENT_OPEN = "<web-content>"
WEB_CONTENT_CLOSE = "</web-content>"

_SNAPSHOT_LINK = re.compile(r"^- \[Snapshot\]\(([^)\s]+\.yml)\)\s*$", re.MULTILINE)
_PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.MULTILINE)
_CURRENT_TAB = re.compile(r"^- (\d+): \(current\)", re.MULTILINE)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

logger = logging.getLogger(__name__)


class MCPSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """``{"content": [...], "isError": bool}`` as MCP returns it."""
        ...

    async def aclose(self) -> None: ...


MCPConnector = Callable[[], Awaitable[MCPSession]]


class PlaywrightMCPSession:
    """One MCP session to Playwright MCP, owned by a task of its own.

    The MCP client's streams must be opened and closed in one task, while the
    daemon's calls arrive on many, so calls go to that task through a queue.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())

    @classmethod
    async def open(cls, url: str) -> "PlaywrightMCPSession":
        session = cls(url)
        await session._ready
        return session

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await self._queue.put((name, arguments, future))
        return await future

    async def aclose(self) -> None:
        await self._queue.put(None)
        with contextlib.suppress(Exception):
            await self._task

    async def _run(self) -> None:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async with streamable_http_client(self._url) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    self._ready.set_result(None)
                    while (item := await self._queue.get()) is not None:
                        name, arguments, future = item
                        try:
                            result = await session.call_tool(name, arguments)
                            future.set_result(result.model_dump(mode="json"))
                        except Exception as exc:  # reported to the caller
                            future.set_exception(exc)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not None and not item[2].done():
                    item[2].set_exception(RuntimeError(f"MCP session closed: {exc}"))


@dataclass
class _Request:
    session: MCPSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class CallResult:
    content: str
    images: list[dict[str, str]]
    page_url: str | None
    is_error: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "images": self.images,
            "page_url": self.page_url,
            "is_error": self.is_error,
        }


def shape_result(raw: dict[str, Any], *, output_root: Path) -> CallResult:
    """Playwright MCP's result as the model reads it.

    Snapshot files the result names are read into it, and the generated
    Playwright code is dropped. Images are kept as ``{media_type, data}``.
    """
    texts: list[str] = []
    images: list[dict[str, str]] = []
    for part in raw.get("content") or []:
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image" and part.get("data"):
            images.append(
                {"media_type": str(part.get("mimeType") or "image/png"), "data": str(part["data"])}
            )
    text = _drop_section("\n".join(texts), "Ran Playwright code")

    def _inline(match: re.Match[str]) -> str:
        path = (output_root / match.group(1)).resolve()
        if output_root.resolve() not in path.parents:
            return match.group(0)
        try:
            return "```yaml\n" + path.read_text().rstrip() + "\n```"
        except OSError:
            return match.group(0)

    text = _SNAPSHOT_LINK.sub(_inline, text)
    page_url = _PAGE_URL.search(text)
    return CallResult(
        content=text.strip(),
        images=images,
        page_url=page_url.group(1) if page_url else None,
        is_error=bool(raw.get("isError")),
    )


def _drop_section(text: str, heading: str) -> str:
    sections = re.split(r"(?m)^(?=### )", text)
    return "".join(s for s in sections if not s.startswith(f"### {heading}"))


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


class _BearerAuth:
    """Require ``Authorization: Bearer <token>`` on HTTP and websockets, but /health."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if hmac.compare_digest(supplied, self._expected):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4401})
            return
        response = _json_error(401, "UNAUTHORIZED", "Missing or invalid bearer token")
        await response(scope, receive, send)


class BrowserDaemon:
    """The app of one user's browser."""

    def __init__(
        self,
        *,
        token: str,
        connect: MCPConnector,
        cdp: ChromiumCDP | None,
        home: Path = Path(BROWSER_HOME),
        novnc_dir: Path | None = None,
        vnc_port: int = 5900,
        session_idle_seconds: float = SESSION_IDLE_SECONDS,
        download_wait_seconds: float = DOWNLOAD_WAIT_SECONDS,
        processes: Any = None,
    ) -> None:
        if not str(token or "").strip():
            raise ValueError("browser token is required")
        self._connect = connect
        self.cdp = cdp
        # Xvfb, Chromium, the VNC server and Playwright MCP (processes.py), when
        # this daemon runs them.
        self._processes = processes
        self.home = home
        self.uploads_dir = home / "uploads"
        self._vnc_port = vnc_port
        self._idle_seconds = float(session_idle_seconds)
        self._download_wait = float(download_wait_seconds)
        self._requests: dict[str, _Request] = {}
        self._open_lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        routes: list[Any] = [
            Route("/health", self.health, methods=["GET"]),
            Route("/requests/{request_id}/call", self.call, methods=["POST"]),
            Route("/requests/{request_id}/upload", self.upload, methods=["POST"]),
            Route("/requests/{request_id}/front", self.front, methods=["POST"]),
            Route("/requests/{request_id}", self.release, methods=["DELETE"]),
            Route("/downloads/{download_id}", self.download, methods=["GET"]),
            Route("/sites", self.sites, methods=["GET"]),
            Route("/sites/{domain}", self.clear_site, methods=["DELETE"]),
            WebSocketRoute("/view/websockify", self.view_socket),
            WebSocketRoute("/cdp", self.cdp_socket),
        ]
        if novnc_dir is not None and novnc_dir.is_dir():
            routes.append(Mount("/view", app=StaticFiles(directory=str(novnc_dir), html=True)))
        self.app = Starlette(routes=routes, lifespan=self._lifespan)
        self.app.add_middleware(_BearerAuth, token=token)

    @contextlib.asynccontextmanager
    async def _lifespan(self, app: Any):
        del app
        if self._processes is not None:
            await self._processes.start()
        if self.cdp is not None:
            await self.cdp.start()
        self._reaper = asyncio.create_task(self._reap_forever())
        try:
            yield
        finally:
            self._reaper.cancel()
            for request_id in list(self._requests):
                await self._close(request_id)
            if self._processes is not None and self.cdp is not None:
                # Chromium batches cookie writes and drops them on a signal;
                # closing it over CDP runs its own shutdown, which writes the
                # profile out.
                self._processes.begin_stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.cdp.call("Browser.close"), timeout=10.0)
                await self._processes.wait_exit("chromium", timeout=15.0)
            if self.cdp is not None:
                await self.cdp.close()
            if self._processes is not None:
                await self._processes.stop()

    async def health(self, request: Request) -> Response:
        """``200`` once the browser can take calls; ``503`` while its children start."""
        del request
        if self._processes is not None and not await self._processes.ready():
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def call(self, request: Request) -> Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")
        tool = body.get("tool")
        if tool not in RELAYED_TOOLS:
            return _json_error(400, "UNKNOWN_TOOL", f"'{tool}' is not a browser tool")
        arguments = body.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _json_error(400, "INVALID_REQUEST", "arguments must be an object")
        sites = _parse_sites(body.get("sites"))
        if sites is None:
            return _json_error(400, "INVALID_REQUEST", "sites must be a list of origins")
        result = await self.run_tool(request.path_params["request_id"], tool, arguments, sites)
        return JSONResponse(result.to_json())

    async def run_tool(
        self, request_id: str, tool: str, arguments: dict[str, Any], sites: Sequence[str]
    ) -> CallResult:
        """Run ``tool`` in the request's tab, keeping it on ``sites``."""
        url = arguments.get("url")
        if tool in ("browser_navigate", "browser_tabs") and isinstance(url, str) and url:
            if not url_allowed(url, sites):
                return _off_sites_error(url, sites)
        try:
            entry = await self._entry(request_id)
        except Exception as exc:
            return CallResult(f"The browser is not ready: {exc}", [], None, True)
        async with entry.lock:
            entry.last_used = time.monotonic()
            started = time.monotonic()
            try:
                raw = await entry.session.call_tool(tool, arguments)
            except Exception as exc:
                # The session is broken; the request's next call opens another.
                self._requests.pop(request_id, None)
                with contextlib.suppress(Exception):
                    await entry.session.aclose()
                return CallResult(f"Browser call failed: {exc}", [], None, True)
            result = shape_result(raw, output_root=self.home)
            if result.page_url and not url_allowed(result.page_url, sites):
                await self._leave(entry.session, sites)
                return _off_sites_error(result.page_url, sites)
            downloads = await self._downloads_since(started)
        if downloads:
            result.content += "\n### Downloads\n" + "\n".join(
                f"- download_id: {d.id}, file: {d.file_name}, state: {d.state}"
                for d in downloads
            )
        if not result.is_error and result.content:
            result.content = f"{WEB_CONTENT_OPEN}\n{result.content}\n{WEB_CONTENT_CLOSE}"
        return result

    async def upload(self, request: Request) -> Response:
        request_id = request.path_params["request_id"]
        params = request.query_params
        name = _SAFE_NAME.sub("_", params.get("name") or "").strip("._") or "upload"
        target = params.get("target") or ""
        if not target:
            return _json_error(400, "INVALID_REQUEST", "target is required")
        sites = _parse_sites(_json_or_none(params.get("sites") or "[]"))
        if sites is None:
            return _json_error(400, "INVALID_REQUEST", "sites must be a list of origins")
        data = await request.body()
        if len(data) > MAX_UPLOAD_BYTES:
            return _json_error(413, "FILE_TOO_LARGE", f"uploads are limited to {MAX_UPLOAD_BYTES} bytes")
        folder = self.uploads_dir / _SAFE_NAME.sub("_", request_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        opened = await self.run_tool(
            request_id,
            "browser_click",
            {"target": target, "element": params.get("element") or "file input"},
            sites,
        )
        if opened.is_error:
            return JSONResponse(opened.to_json())
        entry = self._requests.get(request_id)
        if entry is None:
            return JSONResponse(CallResult("The browser session ended", [], None, True).to_json())
        async with entry.lock:
            raw = await entry.session.call_tool("browser_file_upload", {"paths": [str(path)]})
        result = shape_result(raw, output_root=self.home)
        if not result.is_error:
            result.content = f"{WEB_CONTENT_OPEN}\n{result.content}\n{WEB_CONTENT_CLOSE}"
        return JSONResponse(result.to_json())

    async def front(self, request: Request) -> Response:
        """Bring the request's tab to the front, so the user's live view shows it."""
        entry = self._requests.get(request.path_params["request_id"])
        if entry is None:
            return JSONResponse({"front": False})
        async with entry.lock:
            listed = shape_result(
                await entry.session.call_tool("browser_tabs", {"action": "list"}),
                output_root=self.home,
            )
            current = _CURRENT_TAB.search(listed.content)
            if current is not None:
                await entry.session.call_tool(
                    "browser_tabs", {"action": "select", "index": int(current.group(1))}
                )
        return JSONResponse({"front": current is not None})

    async def release(self, request: Request) -> Response:
        closed = await self._close(request.path_params["request_id"])
        return JSONResponse({"closed": closed})

    async def download(self, request: Request) -> Response:
        download_id = request.path_params["download_id"]
        path = self.cdp.download_path(download_id) if self.cdp is not None else None
        if path is None or not path.is_file():
            return _json_error(404, "DOWNLOAD_NOT_FOUND", f"no finished download '{download_id}'")
        download = self.cdp.downloads[download_id]
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"X-Mash-File-Name": download.file_name},
        )

    async def sites(self, request: Request) -> Response:
        del request
        if self.cdp is None:
            return JSONResponse({"sites": []})
        return JSONResponse({"sites": await self.cdp.cookie_domains()})

    async def clear_site(self, request: Request) -> Response:
        domain = request.path_params["domain"]
        if self.cdp is None:
            return JSONResponse({"domain": domain, "cookies": 0})
        return JSONResponse({"domain": domain, "cookies": await self.cdp.clear_domain(domain)})

    async def view_socket(self, websocket: WebSocket) -> None:
        """noVNC's websocket, as raw bytes to and from the VNC server."""
        await websocket.accept(subprotocol=_binary_subprotocol(websocket))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self._vnc_port)
        except OSError:
            await websocket.close(code=1011)
            return

        async def to_vnc() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes") or (message.get("text") or "").encode()
                writer.write(data)
                await writer.drain()

        async def from_vnc() -> None:
            while data := await reader.read(65536):
                await websocket.send_bytes(data)

        await _pump(to_vnc(), from_vnc())
        writer.close()
        with contextlib.suppress(Exception):
            await websocket.close()

    async def cdp_socket(self, websocket: WebSocket) -> None:
        """A websocket to Chromium's browser-level CDP endpoint."""
        if self.cdp is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        try:
            upstream = await websockets.connect(await self.cdp.browser_ws_url(), max_size=None)
        except Exception:
            await websocket.close(code=1011)
            return

        async def to_chromium() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await upstream.send(message.get("text") or message.get("bytes") or b"")

        async def from_chromium() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        await _pump(to_chromium(), from_chromium())
        await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()

    async def _entry(self, request_id: str) -> _Request:
        async with self._open_lock:
            entry = self._requests.get(request_id)
            if entry is None:
                session = await self._connect()
                # Sessions share Chromium's tabs; a new one keeps this request's
                # actions off every other request's tab.
                await session.call_tool("browser_tabs", {"action": "new"})
                entry = self._requests[request_id] = _Request(session=session)
            return entry

    async def _close(self, request_id: str) -> bool:
        entry = self._requests.pop(request_id, None)
        if entry is None:
            return False
        async with entry.lock:
            with contextlib.suppress(Exception):
                await entry.session.call_tool("browser_tabs", {"action": "close"})
            with contextlib.suppress(Exception):
                await entry.session.aclose()
        return True

    async def _leave(self, session: MCPSession, sites: Sequence[str]) -> None:
        """Go back from a page outside ``sites``, or to a blank page."""
        with contextlib.suppress(Exception):
            back = shape_result(
                await session.call_tool("browser_navigate_back", {}), output_root=self.home
            )
            if back.page_url and url_allowed(back.page_url, sites):
                return
        with contextlib.suppress(Exception):
            await session.call_tool("browser_navigate", {"url": "about:blank"})

    async def _downloads_since(self, started: float) -> list[Any]:
        if self.cdp is None:
            return []
        deadline = time.monotonic() + self._download_wait
        while True:
            downloads = self.cdp.downloads_since(started)
            if all(d.state != "inProgress" for d in downloads) or time.monotonic() >= deadline:
                return downloads
            await asyncio.sleep(0.2)

    async def reap_idle(self) -> list[str]:
        now = time.monotonic()
        idle = [
            request_id
            for request_id, entry in self._requests.items()
            if now - entry.last_used >= self._idle_seconds and not entry.lock.locked()
        ]
        for request_id in idle:
            await self._close(request_id)
        return idle

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self._idle_seconds / 4))
            try:
                await self.reap_idle()
            except Exception:  # pragma: no cover - logged, retried next pass
                logger.exception("closing idle browser sessions failed")


def _off_sites_error(url: str, sites: Sequence[str]) -> CallResult:
    allowed = ", ".join(sites) or "none"
    return CallResult(
        f"{origin_of(url)} is outside the sites this agent may open ({allowed}). "
        "The page was not shown; stay on the allowed sites.",
        [],
        None,
        True,
    )


def _parse_sites(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    try:
        return check_sites(value)
    except ValueError:
        return None


def _json_or_none(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _binary_subprotocol(websocket: WebSocket) -> str | None:
    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    return "binary" if "binary" in offered else None


async def _pump(*coroutines: Awaitable[None]) -> None:
    """Run both directions of a bridge until either ends."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def resolve_browser_token(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_BROWSER_TOKEN_ENV) or "").strip()
    return value or None


__all__ = [
    "BROWSER_HOME",
    "BROWSER_PORT",
    "BrowserDaemon",
    "CallResult",
    "MASH_BROWSER_TOKEN_ENV",
    "MCPSession",
    "PlaywrightMCPSession",
    "WEB_CONTENT_CLOSE",
    "WEB_CONTENT_OPEN",
    "resolve_browser_token",
    "shape_result",
]

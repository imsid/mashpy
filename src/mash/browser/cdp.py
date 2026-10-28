"""The daemon's own CDP connection to Chromium: downloads and site data.

Playwright MCP attached over CDP does not manage downloads, so they would land
in Chromium's default folder. ``ChromiumCDP`` points them at the daemon's
downloads folder (``Browser.setDownloadBehavior``), records each one from
``Browser.downloadWillBegin`` and ``Browser.downloadProgress``, and reads and
clears cookies for the management API. It reconnects when Chromium restarts.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

_RECONNECT_SECONDS = 1.0
_CALL_TIMEOUT = 30.0


@dataclass
class Download:
    """One file a page downloaded, saved as ``<downloads>/<id>``."""

    id: str
    file_name: str
    url: str
    started_at: float
    state: str = "inProgress"


class ChromiumCDP:
    """A browser-level CDP session to the Chromium at ``cdp_url``."""

    def __init__(self, cdp_url: str, *, downloads_dir: Path) -> None:
        self._cdp_url = cdp_url.rstrip("/")
        self.downloads_dir = downloads_dir
        self.downloads: dict[str, Download] = {}
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def browser_ws_url(self) -> str:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self._cdp_url}/json/version")
            response.raise_for_status()
        return str(response.json()["webSocketDebuggerUrl"])

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.wait_for(self._connected.wait(), timeout=_CALL_TIMEOUT)
        message_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            reply = await asyncio.wait_for(future, timeout=_CALL_TIMEOUT)
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            raise RuntimeError(f"{method} failed: {reply['error'].get('message')}")
        return dict(reply.get("result") or {})

    def downloads_since(self, started: float) -> list[Download]:
        return [d for d in self.downloads.values() if d.started_at >= started]

    def download_path(self, download_id: str) -> Path | None:
        download = self.downloads.get(download_id)
        if download is None or download.state != "completed":
            return None
        return self.downloads_dir / download.id

    async def cookie_domains(self) -> list[dict[str, Any]]:
        """``[{domain, cookies}]``: the sites the profile holds cookies for."""
        counts: dict[str, int] = {}
        for cookie in (await self.call("Storage.getCookies")).get("cookies") or []:
            domain = str(cookie.get("domain") or "").lstrip(".")
            if domain:
                counts[domain] = counts.get(domain, 0) + 1
        return [{"domain": domain, "cookies": n} for domain, n in sorted(counts.items())]

    async def clear_domain(self, domain: str) -> int:
        """Delete the cookies of ``domain`` and its subdomains, and its storage."""
        domain = domain.strip().lstrip(".").lower()
        cookies = (await self.call("Storage.getCookies")).get("cookies") or []
        doomed = [
            c
            for c in cookies
            if (d := str(c.get("domain") or "").lstrip(".").lower()) == domain
            or d.endswith(f".{domain}")
        ]
        if doomed:
            # An expired cookie replaces and deletes the stored one.
            await self.call(
                "Storage.setCookies",
                {
                    "cookies": [
                        {
                            "name": c["name"],
                            "value": "",
                            "domain": c["domain"],
                            "path": c.get("path") or "/",
                            "secure": bool(c.get("secure")),
                            "expires": 1,
                        }
                        for c in doomed
                    ]
                },
            )
        for scheme in ("https", "http"):
            await self.call(
                "Storage.clearDataForOrigin",
                {"origin": f"{scheme}://{domain}", "storageTypes": "all"},
            )
        return len(doomed)

    async def _run(self) -> None:
        while True:
            try:
                url = await self.browser_ws_url()
                async with websockets.connect(url, max_size=None) as ws:
                    self._ws = ws
                    reader = asyncio.create_task(self._read(ws))
                    await self._send_raw(
                        ws,
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allowAndName",
                            "downloadPath": str(self.downloads_dir),
                            "eventsEnabled": True,
                        },
                    )
                    self._connected.set()
                    await reader
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Chromium is starting or restarting.
                logger.debug("CDP connection to %s: %s", self._cdp_url, exc)
            finally:
                self._connected.clear()
                self._ws = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_result({"error": {"message": "CDP connection closed"}})
            await asyncio.sleep(_RECONNECT_SECONDS)

    async def _send_raw(self, ws: Any, method: str, params: dict[str, Any]) -> None:
        message_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        reply = await asyncio.wait_for(future, timeout=_CALL_TIMEOUT)
        if "error" in reply:
            raise RuntimeError(f"{method} failed: {reply['error'].get('message')}")

    async def _read(self, ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if "id" in message:
                future = self._pending.get(message["id"])
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            self._on_event(str(message.get("method") or ""), message.get("params") or {})

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Browser.downloadWillBegin":
            guid = str(params.get("guid") or "")
            if guid:
                self.downloads[guid] = Download(
                    id=guid,
                    file_name=str(params.get("suggestedFilename") or guid),
                    url=str(params.get("url") or ""),
                    started_at=time.monotonic(),
                )
        elif method == "Browser.downloadProgress":
            download = self.downloads.get(str(params.get("guid") or ""))
            if download is not None:
                download.state = str(params.get("state") or download.state)


__all__ = ["ChromiumCDP", "Download"]

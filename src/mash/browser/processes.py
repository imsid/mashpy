"""The processes a browser container runs next to its daemon.

``mash browser serve`` starts them as its children and restarts any that
exits: a virtual display (Xvfb), Chromium drawing to it with the user's
profile, a VNC server for the live view, and Playwright MCP attached to
Chromium over CDP. All of them listen on ``127.0.0.1`` only; the daemon is
the container's one reachable port.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import PLAYWRIGHT_MCP_VERSION

logger = logging.getLogger(__name__)

MASH_BROWSER_CHROMIUM_ENV = "MASH_BROWSER_CHROMIUM"
DISPLAY = ":99"
SCREEN = "1280x800x24"
CDP_PORT = 9222
MCP_PORT = 8931
VNC_PORT = 5900
# Playwright MCP's snapshots and screenshots: evicted past this size.
OUTPUT_MAX_BYTES = 50 * 1024 * 1024
_RESTART_DELAY = 1.0


def find_chromium() -> str:
    """The Chromium build the image installed for the pinned Playwright MCP."""
    explicit = os.environ.get(MASH_BROWSER_CHROMIUM_ENV, "").strip()
    if explicit:
        return explicit
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/ms-playwright"
    found = sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"))
    if not found:
        raise RuntimeError(f"no Chromium under {root}; set {MASH_BROWSER_CHROMIUM_ENV}")
    return found[-1]


@dataclass(frozen=True)
class BrowserLayout:
    """Where a browser's files live, under the user's volume."""

    home: Path

    @property
    def profile(self) -> Path:
        return self.home / "profile"

    @property
    def downloads(self) -> Path:
        return self.home / "downloads"

    @property
    def output(self) -> Path:
        return self.home / "output"


def child_commands(layout: BrowserLayout, *, chromium: str) -> dict[str, list[str]]:
    """Each child's argv, by name, in start order."""
    width, height, _ = SCREEN.split("x")
    return {
        "xvfb": ["Xvfb", DISPLAY, "-screen", "0", SCREEN, "-nolisten", "tcp"],
        "chromium": [
            chromium,
            f"--user-data-dir={layout.profile}",
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            # The container is the boundary, and Docker's seccomp profile
            # blocks the namespaces Chromium's own sandbox needs.
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            # No banners about the build or the flags, no sign-in prompt, and
            # less background traffic. Chromium still makes some calls of its
            # own on a fresh profile (account check, push registration).
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-default-apps",
            "--test-type",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            f"--window-size={width},{height}",
            "--window-position=0,0",
            "--start-maximized",
            "about:blank",
        ],
        "x11vnc": [
            "x11vnc", "-display", DISPLAY, "-localhost", "-rfbport", str(VNC_PORT),
            "-forever", "-shared", "-nopw", "-quiet",
        ],
        "playwright-mcp": [
            "playwright-mcp",
            "--cdp-endpoint", f"http://127.0.0.1:{CDP_PORT}",
            # Binds localhost and answers only Host: localhost, its defaults.
            "--port", str(MCP_PORT),
            "--caps=vision",
            "--output-dir", str(layout.output),
            "--output-max-size", str(OUTPUT_MAX_BYTES),
            "--image-responses", "allow",
        ],
    }


class BrowserProcesses:
    """Start the children in order and restart any that exits."""

    def __init__(self, layout: BrowserLayout, *, chromium: str | None = None) -> None:
        self.layout = layout
        self._chromium = chromium
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._running: dict[str, asyncio.subprocess.Process] = {}

    async def start(self) -> None:
        for folder in (self.layout.profile, self.layout.downloads, self.layout.output):
            folder.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DISPLAY": DISPLAY}
        commands = child_commands(self.layout, chromium=self._chromium or find_chromium())
        for name, argv in commands.items():
            self._tasks.append(asyncio.create_task(self._keep_running(name, argv, env)))
            # Each child needs the one before it: the display, then Chromium.
            await asyncio.sleep(0.5)

    def begin_stop(self) -> None:
        """Restart no child that exits from now on."""
        self._stopping = True

    async def wait_exit(self, name: str, timeout: float) -> bool:
        """Wait for a child to exit on its own; whether it did in time."""
        process = self._running.get(name)
        if process is None:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Stop the children one at a time, last started first."""
        self._stopping = True
        for task in reversed(self._tasks):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._tasks.clear()

    async def ready(self) -> bool:
        """Whether Chromium's CDP and Playwright MCP both answer."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.get(f"http://127.0.0.1:{CDP_PORT}/json/version")
                await client.get(f"http://localhost:{MCP_PORT}/mcp")
        except httpx.HTTPError:
            return False
        return True

    async def _keep_running(self, name: str, argv: list[str], env: dict[str, str]) -> None:
        while True:
            # Playwright MCP resolves output paths against its working folder.
            process = await asyncio.create_subprocess_exec(
                *argv, env=env, cwd=str(self.layout.home)
            )
            self._running[name] = process
            try:
                code = await process.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                raise
            if self._stopping:
                return
            logger.warning("%s exited with %s; restarting", name, code)
            await asyncio.sleep(_RESTART_DELAY)


__all__ = [
    "BrowserLayout",
    "BrowserProcesses",
    "CDP_PORT",
    "MCP_PORT",
    "PLAYWRIGHT_MCP_VERSION",
    "VNC_PORT",
    "child_commands",
    "find_chromium",
]

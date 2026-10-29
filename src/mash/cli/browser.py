"""``mash browser``: serve and manage per-user browsers.

``serve`` runs inside a browser container. ``ls`` and ``stop`` run on the
Docker host and find browsers by their ``mash.browser.user`` label, so no host
connection is needed; ``--namespace`` (or ``MASH_SANDBOX_NAMESPACE``) picks a
deployment's browsers. ``view`` prints a live-view link to a user's browser,
signed with ``MASH_HOST_SECRET``, for the address in ``MASH_PUBLIC_URL``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from mash.browser.daemon import (
    BROWSER_HOME,
    BROWSER_PORT,
    MASH_BROWSER_TOKEN_ENV,
    BrowserDaemon,
    PlaywrightMCPSession,
    resolve_browser_token,
)
from mash.browser.manager import DEFAULT_HANDOFF_SECONDS
from mash.browser.provider import BROWSER_USER_LABEL, browser_name
from mash.browser.tokens import make_view_token
from mash.sandbox.docker_engine import DockerEngine, DockerEngineError
from mash.sandbox.provider import NAMESPACE_LABEL, resolve_namespace

from .render import RichRenderer
from .sandbox import _uptime

DEFAULT_NOVNC_DIR = "/usr/share/novnc"


def add_browser_parser(subparsers) -> None:
    browser = subparsers.add_parser("browser", help="Serve and manage per-user browsers")
    browser_subparsers = browser.add_subparsers(dest="browser_command")
    browser.set_defaults(handler=lambda _: (browser.print_help(), 0)[1])

    serve = browser_subparsers.add_parser(
        "serve", help="Run the browser daemon (inside a browser container)"
    )
    serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve.add_argument("--port", type=int, default=BROWSER_PORT, help="Bind port")
    serve.add_argument("--home", default=BROWSER_HOME, help="The browser's data folder")
    serve.add_argument("--novnc-dir", default=DEFAULT_NOVNC_DIR, help="noVNC's web client")
    serve.add_argument(
        "--cdp-url",
        default=None,
        help="Use a running Chromium's CDP endpoint instead of starting one",
    )
    serve.add_argument(
        "--mcp-url",
        default=None,
        help="Use a running Playwright MCP's /mcp endpoint instead of starting one",
    )
    serve.set_defaults(handler=_run_serve)

    ls = browser_subparsers.add_parser("ls", help="List browsers")
    _add_namespace_argument(ls)
    ls.set_defaults(handler=_run_ls)

    view = browser_subparsers.add_parser(
        "view", help="Print a live-view link to a user's browser"
    )
    view.add_argument("user_id")
    view.add_argument(
        "--public-url",
        default=None,
        help="The address users reach the host at (or set MASH_PUBLIC_URL)",
    )
    view.set_defaults(handler=_run_view)

    stop = browser_subparsers.add_parser(
        "stop", help="Stop a user's browser; its profile is kept"
    )
    stop.add_argument("user_id")
    _add_namespace_argument(stop)
    stop.set_defaults(handler=_run_stop)


def _add_namespace_argument(parser) -> None:
    parser.add_argument(
        "--namespace",
        default=None,
        help="Deployment namespace (or set MASH_SANDBOX_NAMESPACE; default mash)",
    )


def _run_serve(args) -> int:
    import uvicorn

    from mash.browser.cdp import ChromiumCDP
    from mash.browser.processes import CDP_PORT, MCP_PORT, BrowserLayout, BrowserProcesses

    token = resolve_browser_token()
    if token is None:
        raise SystemExit(f"browser token is required. Set {MASH_BROWSER_TOKEN_ENV}.")
    layout = BrowserLayout(home=Path(args.home))
    external = bool(args.cdp_url or args.mcp_url)
    cdp_url = args.cdp_url or f"http://127.0.0.1:{CDP_PORT}"
    mcp_url = args.mcp_url or f"http://localhost:{MCP_PORT}/mcp"

    async def connect() -> PlaywrightMCPSession:
        return await PlaywrightMCPSession.open(mcp_url)

    daemon = BrowserDaemon(
        token=token,
        connect=connect,
        cdp=ChromiumCDP(cdp_url, downloads_dir=layout.downloads),
        home=layout.home,
        novnc_dir=Path(args.novnc_dir),
        processes=None if external else BrowserProcesses(layout),
    )
    uvicorn.run(daemon.app, host=args.host, port=args.port)
    return 0


async def _list_rows(namespace: str) -> list[list[str]]:
    engine = DockerEngine()
    try:
        rows: list[list[str]] = []
        containers = await engine.list_containers(
            labels=[BROWSER_USER_LABEL, f"{NAMESPACE_LABEL}={namespace}"]
        )
        for summary in sorted(containers, key=lambda c: c["Labels"][BROWSER_USER_LABEL]):
            details = await engine.inspect_container(summary["Id"]) or {}
            state = details.get("State") or {}
            rows.append(
                [
                    summary["Labels"][BROWSER_USER_LABEL],
                    str(state.get("Status") or summary.get("State") or ""),
                    _uptime(str(state.get("StartedAt") or ""), bool(state.get("Running"))),
                    str(details.get("Name") or "").lstrip("/"),
                ]
            )
        return rows
    finally:
        await engine.close()


def _run_ls(args) -> int:
    renderer = RichRenderer()
    try:
        rows = asyncio.run(_list_rows(resolve_namespace(args.namespace)))
    except (DockerEngineError, ValueError) as exc:
        renderer.error(str(exc))
        return 1
    if not rows:
        renderer.info("No browsers.")
        return 0
    renderer.table(["User", "Status", "Uptime", "Container and volume"], rows)
    return 0


def _run_view(args) -> int:
    renderer = RichRenderer()
    secret = os.environ.get("MASH_HOST_SECRET", "").strip()
    public_url = str(args.public_url or os.environ.get("MASH_PUBLIC_URL") or "").strip()
    if not secret:
        renderer.error("MASH_HOST_SECRET is required to sign the link")
        return 1
    if not public_url:
        renderer.error("set --public-url or MASH_PUBLIC_URL to the address users reach the host at")
        return 1
    expires_at = int(time.time()) + DEFAULT_HANDOFF_SECONDS
    token = make_view_token(secret, args.user_id, "", expires_at)
    renderer.link(f"{public_url.rstrip('/')}/s/{token}/browser/")
    renderer.info(f"Valid for {DEFAULT_HANDOFF_SECONDS // 60} minutes.")
    return 0


def _run_stop(args) -> int:
    renderer = RichRenderer()

    async def _stop(name: str) -> None:
        engine = DockerEngine()
        try:
            await engine.stop_container(name)
        finally:
            await engine.close()

    try:
        name = browser_name(args.user_id, resolve_namespace(args.namespace))
        asyncio.run(_stop(name))
    except (DockerEngineError, ValueError) as exc:
        renderer.error(str(exc))
        return 1
    renderer.info(f"Stopped {name}; its profile is kept.")
    return 0


__all__ = ["add_browser_parser"]

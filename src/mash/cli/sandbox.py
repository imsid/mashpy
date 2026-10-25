"""``mash sandbox``: serve and manage per-user sandboxes.

``serve`` runs inside a sandbox. ``ls``, ``shell`` and ``stop`` run on the
Docker host: they find sandboxes by their ``mash.sandbox.user`` label, so no
host connection is needed. ``--namespace`` (or ``MASH_SANDBOX_NAMESPACE``)
picks a deployment's sandboxes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from mash.sandbox.daemon import (
    MASH_SANDBOX_TOKEN_ENV,
    SANDBOX_PORT,
    WORKSPACES_ROOT,
    SandboxDaemon,
    resolve_sandbox_token,
)
from mash.sandbox.docker_engine import DockerEngine, DockerEngineError
from mash.sandbox.provider import (
    NAMESPACE_LABEL,
    USER_LABEL,
    resolve_namespace,
    sandbox_name,
)

from .render import RichRenderer

ADMIN_HISTORY = f"{WORKSPACES_ROOT}/.admin_history"


def add_sandbox_parser(subparsers) -> None:
    sandbox = subparsers.add_parser("sandbox", help="Serve and manage per-user sandboxes")
    sandbox_subparsers = sandbox.add_subparsers(dest="sandbox_command")
    sandbox.set_defaults(handler=lambda _: (sandbox.print_help(), 0)[1])

    serve = sandbox_subparsers.add_parser(
        "serve", help="Run the sandbox daemon (inside a sandbox)"
    )
    serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve.add_argument("--port", type=int, default=SANDBOX_PORT, help="Bind port")
    serve.add_argument(
        "--workspaces",
        default=WORKSPACES_ROOT,
        help="Directory holding one folder per agent",
    )
    serve.set_defaults(handler=_run_serve)

    ls = sandbox_subparsers.add_parser("ls", help="List sandboxes")
    _add_namespace_argument(ls)
    ls.set_defaults(handler=_run_ls)

    shell = sandbox_subparsers.add_parser(
        "shell", help="Open a shell in a user's sandbox, starting it if stopped"
    )
    shell.add_argument("user_id")
    shell.add_argument("--agent", default=None, help="Start in this agent's folder")
    shell.add_argument("--root", action="store_true", help="Open a root shell")
    _add_namespace_argument(shell)
    shell.set_defaults(handler=_run_shell)

    stop = sandbox_subparsers.add_parser(
        "stop", help="Stop a user's sandbox; its next use starts it again"
    )
    stop.add_argument("user_id")
    _add_namespace_argument(stop)
    stop.set_defaults(handler=_run_stop)


def _add_namespace_argument(parser) -> None:
    parser.add_argument(
        "--namespace",
        default=None,
        help="Deployment sandbox namespace (or set MASH_SANDBOX_NAMESPACE; default mash)",
    )


def _run_serve(args) -> int:
    import uvicorn

    token = resolve_sandbox_token()
    if token is None:
        raise SystemExit(f"sandbox token is required. Set {MASH_SANDBOX_TOKEN_ENV}.")
    daemon = SandboxDaemon(token=token, workspaces_root=args.workspaces)
    uvicorn.run(daemon.app, host=args.host, port=args.port)
    return 0


def _uptime(started_at: str, running: bool) -> str:
    if not running or not started_at:
        return "-"
    # Docker reports nanoseconds; fromisoformat takes up to microseconds.
    trimmed = started_at.split(".")[0].rstrip("Z")
    started = datetime.fromisoformat(trimmed).replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m"


async def _list_rows(namespace: str) -> list[list[str]]:
    engine = DockerEngine()
    try:
        rows: list[list[str]] = []
        containers = await engine.list_containers(
            labels=[USER_LABEL, f"{NAMESPACE_LABEL}={namespace}"]
        )
        for summary in sorted(containers, key=lambda c: c["Labels"][USER_LABEL]):
            details: dict[str, Any] = await engine.inspect_container(summary["Id"]) or {}
            state = details.get("State") or {}
            rows.append(
                [
                    summary["Labels"][USER_LABEL],
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
        renderer.info("No sandboxes.")
        return 0
    renderer.table(["User", "Status", "Uptime", "Container and volume"], rows)
    return 0


async def _start(name: str) -> None:
    engine = DockerEngine()
    try:
        if await engine.inspect_container(name) is None:
            raise ValueError(f"no sandbox named {name}")
        await engine.start_container(name)
    finally:
        await engine.close()


def _run_shell(args) -> int:
    renderer = RichRenderer()
    if shutil.which("docker") is None:
        renderer.error("the docker CLI is required for `mash sandbox shell`")
        return 1
    try:
        name = sandbox_name(args.user_id, resolve_namespace(args.namespace))
        asyncio.run(_start(name))
    except (DockerEngineError, ValueError) as exc:
        renderer.error(str(exc))
        return 1
    # Pre-create the history file as `agent`, so a root session cannot leave it
    # root-owned and unwritable for the next agent session.
    subprocess.run(
        ["docker", "exec", "-u", "agent", name, "touch", ADMIN_HISTORY],
        check=False,
    )
    os.execvp("docker", _shell_argv(name, args.agent, args.root))
    return 0  # pragma: no cover - execvp does not return


def _shell_argv(name: str, agent_id: str | None, root: bool) -> list[str]:
    folder = f"{WORKSPACES_ROOT}/{agent_id}" if agent_id else WORKSPACES_ROOT
    # docker exec copies the container's environment, token included. Drop it,
    # or code running as `agent` could read it from this shell's
    # /proc/<pid>/environ while the session is open.
    return [
        "docker", "exec", "-it",
        "-u", "root" if root else "agent",
        "-w", folder,
        "-e", f"HISTFILE={ADMIN_HISTORY}",
        "-e", "PROMPT_COMMAND=history -a",
        name, "env", "-u", MASH_SANDBOX_TOKEN_ENV, "/bin/bash",
    ]


def _run_stop(args) -> int:
    renderer = RichRenderer()

    async def _stop(name: str) -> None:
        engine = DockerEngine()
        try:
            await engine.stop_container(name)
        finally:
            await engine.close()

    try:
        name = sandbox_name(args.user_id, resolve_namespace(args.namespace))
        asyncio.run(_stop(name))
    except (DockerEngineError, ValueError) as exc:
        renderer.error(str(exc))
        return 1
    renderer.info(f"Stopped {name}; its volume is kept.")
    return 0


__all__ = ["add_sandbox_parser"]

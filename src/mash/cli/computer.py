"""``mash computer``: inspect and manage agent computers on the local Docker daemon.

These run on the Docker host. Computers are found by the ``mash.agent``
container label, so no host connection is needed. ``--namespace`` (or
``MASH_COMPUTER_NAMESPACE``) picks a deployment's computers; ``ls`` without one
lists every deployment's.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from mash.runtime.host.computers import (
    AGENT_LABEL,
    NAMESPACE_LABEL,
    WORKSPACE_MOUNT,
    container_name,
    replace_computer,
    resolve_namespace,
    workspace_volume,
)
from mash.sandbox.docker_engine import DockerEngine, DockerEngineError

from .render import RichRenderer

ADMIN_HISTORY = f"{WORKSPACE_MOUNT}/.admin_history"


def add_computer_parser(subparsers) -> None:
    computer = subparsers.add_parser("computer", help="Manage agent computers")
    computer_subparsers = computer.add_subparsers(dest="computer_command")
    computer.set_defaults(handler=lambda _: (computer.print_help(), 0)[1])

    ls = computer_subparsers.add_parser("ls", help="List agent computers")
    _add_namespace_argument(ls)
    ls.set_defaults(handler=_run_ls)

    shell = computer_subparsers.add_parser(
        "shell", help="Open a shell on an agent's computer"
    )
    shell.add_argument("agent_id")
    shell.add_argument("--root", action="store_true", help="Open a root shell")
    _add_namespace_argument(shell)
    shell.set_defaults(handler=_run_shell)

    restart = computer_subparsers.add_parser(
        "restart", help="Replace an agent's container, keeping its workspace volume"
    )
    restart.add_argument("agent_id")
    _add_namespace_argument(restart)
    restart.set_defaults(handler=_run_restart)


def _add_namespace_argument(parser) -> None:
    parser.add_argument(
        "--namespace",
        default=None,
        help="Deployment computer namespace (or set MASH_COMPUTER_NAMESPACE)",
    )


def _uptime(started_at: str, running: bool) -> str:
    if not running or not started_at:
        return "-"
    # Docker reports nanoseconds; fromisoformat takes up to microseconds.
    trimmed = started_at.split(".")[0].rstrip("Z")
    started = datetime.fromisoformat(trimmed).replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m"


async def _list_rows(namespace: str | None) -> list[list[str]]:
    engine = DockerEngine()
    try:
        rows: list[list[str]] = []
        label = f"{NAMESPACE_LABEL}={namespace}" if namespace else AGENT_LABEL
        containers = await engine.list_containers(labels=[label])
        for summary in sorted(
            containers,
            key=lambda c: (c["Labels"].get(NAMESPACE_LABEL, ""), c["Labels"].get(AGENT_LABEL, "")),
        ):
            agent_id = summary["Labels"][AGENT_LABEL]
            container_namespace = summary["Labels"].get(NAMESPACE_LABEL) or None
            details: dict[str, Any] = await engine.inspect_container(summary["Id"]) or {}
            state = details.get("State") or {}
            rows.append(
                [
                    container_namespace or "-",
                    agent_id,
                    str(state.get("Status") or summary.get("State") or ""),
                    _uptime(str(state.get("StartedAt") or ""), bool(state.get("Running"))),
                    str(details.get("RestartCount", 0)),
                    workspace_volume(agent_id, container_namespace),
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
        renderer.info("No agent computers.")
        return 0
    renderer.table(["Namespace", "Agent", "Status", "Uptime", "Restarts", "Volume"], rows)
    return 0


def _run_shell(args) -> int:
    renderer = RichRenderer()
    if shutil.which("docker") is None:
        renderer.error("the docker CLI is required for `mash computer shell`")
        return 1
    try:
        name = container_name(args.agent_id, resolve_namespace(args.namespace))
    except ValueError as exc:
        renderer.error(str(exc))
        return 1
    # Pre-create the history file as `agent`, so a root session cannot leave it
    # root-owned and unwritable for the next agent session.
    subprocess.run(
        ["docker", "exec", "-u", "agent", name, "touch", ADMIN_HISTORY],
        check=False,
    )
    os.execvp(
        "docker",
        [
            "docker", "exec", "-it",
            "-u", "root" if args.root else "agent",
            "-w", "/home/agent",
            "-e", f"HISTFILE={ADMIN_HISTORY}",
            "-e", "PROMPT_COMMAND=history -a",
            name, "/bin/bash",
        ],
    )
    return 0  # pragma: no cover - execvp does not return


def _run_restart(args) -> int:
    renderer = RichRenderer()

    async def _replace(namespace: str | None) -> None:
        engine = DockerEngine()
        try:
            await replace_computer(engine, args.agent_id, namespace)
        finally:
            await engine.close()

    try:
        namespace = resolve_namespace(args.namespace)
        asyncio.run(_replace(namespace))
    except (DockerEngineError, ValueError) as exc:
        renderer.error(str(exc))
        return 1
    renderer.info(f"Replaced {container_name(args.agent_id, namespace)}; workspace kept.")
    return 0


__all__ = ["add_computer_parser"]

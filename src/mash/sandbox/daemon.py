"""The sandbox daemon: runs one user's bash commands inside their sandbox.

``mash sandbox serve`` runs this app in each user's sandbox as the ``mash``
user. ``POST /exec`` runs ``bash -c <command>`` as the ``agent`` user in the
calling agent's folder, ``<workspaces>/<agent_id>``, creating it on first use.
Each call is a fresh shell with a clean environment: nothing from the daemon's
environment reaches it.

Every route but ``/health`` requires ``Authorization: Bearer <token>``, the
user's sandbox token (``mash.sandbox.tokens``). The host is the only caller.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import pwd
import re
import signal
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

MASH_SANDBOX_TOKEN_ENV = "MASH_SANDBOX_TOKEN"
SANDBOX_PORT = 9000
WORKSPACES_ROOT = "/home/agent/workspaces"

BASH_DEFAULT_TIMEOUT = 30
BASH_MAX_OUTPUT_LINES = 50
BASH_MAX_OUTPUT_CHARS = 4000

# Bash runs as this user, which holds none of the sandbox's credentials.
AGENT_USER = "agent"
_AGENT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Agent ids name folders, so they are kept to a safe charset.
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Output read back per call; enough to count lines well past the limits.
_READ_LIMIT = 1024 * 1024
# Time between asking a timed-out command to stop and killing what is left.
_KILL_GRACE = 2.0

# Runs as the agent user. The command runs in the background so that a TERM
# (sent by the daemon, relayed by sudo) kills its whole process group: the
# daemon's user may not signal the agent's processes itself.
_WRAPPER = (
    'mkdir -p -- "$1" && cd -- "$1" || exit 1\n'
    "trap 'kill -KILL 0' TERM\n"
    '/bin/bash -c "$2" &\n'
    'wait "$!"\n'
)


def resolve_bash_user() -> Optional[str]:
    """The user bash runs as, or None to run as the current user.

    Bash switches to ``agent`` when that user exists and is not the current
    user, which is the case in a sandbox.
    """
    try:
        entry = pwd.getpwnam(AGENT_USER)
    except KeyError:
        return None
    if entry.pw_uid == os.geteuid():
        return None
    return AGENT_USER


def build_bash_env(user: Optional[str], working_dir: str) -> Dict[str, str]:
    """The clean environment bash starts with.

    Nothing from the daemon's environment reaches bash except locale and,
    when bash runs as the current user, its HOME and PATH.
    """
    if user is not None:
        home = pwd.getpwnam(user).pw_dir
        path = _AGENT_PATH
    else:
        home = os.environ.get("HOME", "/")
        path = os.environ.get("PATH", _AGENT_PATH)
    return {
        "HOME": home,
        "PATH": path,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PWD": working_dir,
    }


def truncate_output(text: str) -> str:
    """Keep the first lines and characters of a command's output."""
    lines = text.splitlines()
    if not lines:
        return ""
    kept = "\n".join(lines[:BASH_MAX_OUTPUT_LINES])
    suffix_parts: list[str] = []
    if len(lines) > BASH_MAX_OUTPUT_LINES:
        suffix_parts.append(f"{len(lines)} total lines")
    if len(kept) > BASH_MAX_OUTPUT_CHARS:
        kept = kept[:BASH_MAX_OUTPUT_CHARS].rstrip()
        suffix_parts.append(f"max {BASH_MAX_OUTPUT_CHARS} chars")
    if suffix_parts:
        return f"{kept}\n\n... Output truncated ({', '.join(suffix_parts)}) ..."
    return kept


@dataclass(frozen=True)
class ExecResult:
    output: str
    exit_code: int | None
    timed_out: bool = False


async def run_command(
    command: str,
    *,
    folder: str,
    timeout: float = BASH_DEFAULT_TIMEOUT,
    user: Optional[str] = None,
) -> ExecResult:
    """Run ``bash -c command`` in ``folder`` as ``user``.

    Returns when bash exits. Output goes to a file rather than a pipe, so a
    process the command leaves running in the background does not hold the
    call open.
    """
    argv = ["/bin/bash", "-c", _WRAPPER, "mash-exec", folder, command]
    if user is not None:
        argv = ["sudo", "-n", "-u", user, *argv]
    with tempfile.TemporaryFile() as out:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            env=build_bash_env(user, folder),
            start_new_session=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _stop(proc)
        out.seek(0)
        text = out.read(_READ_LIMIT).decode("utf-8", errors="replace")
    if timed_out:
        return ExecResult(output=truncate_output(text), exit_code=None, timed_out=True)
    return ExecResult(output=truncate_output(text), exit_code=proc.returncode)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), _KILL_GRACE)
    except ProcessLookupError:
        return
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


class _BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every route but /health."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            response = _json_error(401, "UNAUTHORIZED", "Missing or invalid bearer token")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SandboxDaemon:
    """The ``/health`` and ``/exec`` app of one user's sandbox."""

    def __init__(
        self,
        *,
        token: str,
        workspaces_root: str = WORKSPACES_ROOT,
        user: Optional[str] = None,
    ) -> None:
        if not str(token or "").strip():
            raise ValueError("sandbox token is required")
        self.workspaces_root = workspaces_root
        self.user = resolve_bash_user() if user is None else user
        self.app = Starlette(
            routes=[
                Route("/health", self.health, methods=["GET"]),
                Route("/exec", self.exec, methods=["POST"]),
            ]
        )
        self.app.add_middleware(_BearerAuthMiddleware, token=token)

    async def health(self, request: Request) -> Response:
        del request
        return JSONResponse({"status": "ok"})

    async def exec(self, request: Request) -> Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(payload, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not _AGENT_ID.match(agent_id):
            return _json_error(
                400,
                "INVALID_REQUEST",
                "agent_id must be 1-128 characters of letters, digits, '.', '_' or '-', "
                "starting with a letter or digit",
            )
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return _json_error(400, "INVALID_REQUEST", "command is required")
        timeout = payload.get("timeout", BASH_DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return _json_error(400, "INVALID_REQUEST", "timeout must be a positive number")

        result = await run_command(
            command,
            folder=os.path.join(self.workspaces_root, agent_id),
            timeout=float(timeout),
            user=self.user,
        )
        return JSONResponse(
            {
                "output": result.output,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
            }
        )


def resolve_sandbox_token(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_SANDBOX_TOKEN_ENV) or "").strip()
    return value or None


__all__ = [
    "AGENT_USER",
    "BASH_DEFAULT_TIMEOUT",
    "BASH_MAX_OUTPUT_CHARS",
    "BASH_MAX_OUTPUT_LINES",
    "ExecResult",
    "MASH_SANDBOX_TOKEN_ENV",
    "SANDBOX_PORT",
    "SandboxDaemon",
    "WORKSPACES_ROOT",
    "build_bash_env",
    "resolve_bash_user",
    "resolve_sandbox_token",
    "run_command",
    "truncate_output",
]

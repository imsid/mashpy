"""The sandbox daemon: runs one user's code inside their sandbox.

``mash sandbox serve`` runs this app in each user's sandbox as the ``mash``
user. Everything it does in a workspace it does as the ``agent`` user, in the
calling agent's folder ``<workspaces>/<agent_id>``, creating it on first use:

- ``POST /exec`` runs ``bash -c <command>``, with optional stdin. Each call is a
  fresh shell with a clean environment: nothing from the daemon's environment
  reaches it.
- ``GET`` and ``PUT /files/<agent_id>/<path>`` read and write one file's bytes.
- ``/ports/<n>/<path>`` proxies HTTP to a server listening on port ``n`` in the
  sandbox.

Every route but ``/health`` requires ``Authorization: Bearer <token>``, the
user's sandbox token (``mash.sandbox.tokens``). The host is the only caller.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import posixpath
import pwd
import re
import signal
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

MASH_SANDBOX_TOKEN_ENV = "MASH_SANDBOX_TOKEN"
SANDBOX_PORT = 9000
WORKSPACES_ROOT = "/home/agent/workspaces"

BASH_DEFAULT_TIMEOUT = 30
# Enough to read a whole source file in one call.
BASH_MAX_OUTPUT_CHARS = 30_000
# Largest file /files reads or writes.
MAX_FILE_BYTES = 100 * 1024 * 1024

# Bash runs as this user, which holds none of the sandbox's credentials.
AGENT_USER = "agent"
_AGENT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Agent ids name folders, so they are kept to a safe charset.
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Output read back per call; enough to count lines well past the limits.
_READ_LIMIT = 1024 * 1024
# Time between asking a timed-out command to stop and killing what is left.
_KILL_GRACE = 2.0
# How long a file read or write may take.
_FILE_TIMEOUT = 120.0
# Extra variables a caller may set for a command: the host passes link URLs.
_EXTRA_ENV_NAME = re.compile(r"^MASH_[A-Z0-9_]{1,64}$")
# Headers that describe one hop, not the message; never forwarded by the proxy.
_HOP_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-encoding",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Runs as the agent user. The command runs in the background so that a TERM
# (sent by the daemon, relayed by sudo) kills its whole process group: the
# daemon's user may not signal the agent's processes itself. A background
# command gets /dev/null as stdin unless stdin is redirected explicitly.
_WRAPPER = (
    'mkdir -p -- "$1" && cd -- "$1" || exit 1\n'
    "trap 'kill -KILL 0' TERM\n"
    '/bin/bash -c "$2" <&0 &\n'
    'wait "$!"\n'
)
# Runs as the agent user: read or write one file in the agent's folder.
_READ_FILE = 'cd -- "$1" 2>/dev/null && test -f "$2" && exec cat -- "$2"\nexit 44\n'
_WRITE_FILE = (
    'mkdir -p -- "$1" && cd -- "$1" || exit 1\n'
    'mkdir -p -- "$(dirname -- "$2")" && exec cat > "$2"\n'
)
# Exit status of _READ_FILE when the path is not a regular file.
_NOT_A_FILE = 44


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


def build_bash_env(
    user: Optional[str],
    working_dir: str,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The clean environment bash starts with.

    Nothing from the daemon's environment reaches bash except locale and,
    when bash runs as the current user, its HOME and PATH. Python packages
    installed with ``pip install --user`` go to ``<folder>/.local``, so they
    persist with the agent's folder. ``extra`` adds the caller's ``MASH_*``
    variables.
    """
    if user is not None:
        home = pwd.getpwnam(user).pw_dir
        path = _AGENT_PATH
    else:
        home = os.environ.get("HOME", "/")
        path = os.environ.get("PATH", _AGENT_PATH)
    user_base = os.path.join(working_dir, ".local")
    env = {
        "HOME": home,
        "PATH": f"{os.path.join(user_base, 'bin')}:{path}",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PWD": working_dir,
        "PYTHONUSERBASE": user_base,
    }
    env.update(extra or {})
    return env


def truncate_output(text: str) -> str:
    """Keep the first characters of a command's output."""
    text = text.rstrip("\n")
    if len(text) <= BASH_MAX_OUTPUT_CHARS:
        return text
    kept = text[:BASH_MAX_OUTPUT_CHARS].rstrip()
    return f"{kept}\n\n... Output truncated (first {BASH_MAX_OUTPUT_CHARS} of {len(text)} chars) ..."


@dataclass(frozen=True)
class ExecResult:
    output: str
    exit_code: int | None
    timed_out: bool = False


# sudo drops the caller's environment, so the variables travel as arguments
# after the script's two and are exported before it runs.
_EXPORT_ENV = 'for kv in "${@:3}"; do export "$kv"; done\n'


def _agent_argv(
    script: str, user: Optional[str], folder: str, arg: str, env: Mapping[str, str]
) -> list[str]:
    argv = [
        "/bin/bash",
        "-c",
        _EXPORT_ENV + script,
        "mash-exec",
        folder,
        arg,
        *(f"{name}={value}" for name, value in env.items()),
    ]
    if user is not None:
        argv = ["sudo", "-n", "-u", user, *argv]
    return argv


async def run_command(
    command: str,
    *,
    folder: str,
    timeout: float = BASH_DEFAULT_TIMEOUT,
    user: Optional[str] = None,
    stdin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ExecResult:
    """Run ``bash -c command`` in ``folder`` as ``user``.

    Returns when bash exits. Output goes to a file rather than a pipe, so a
    process the command leaves running in the background does not hold the
    call open. ``stdin`` is fed to the command; without it, stdin is empty.
    """
    bash_env = build_bash_env(user, folder, env)
    argv = _agent_argv(_WRAPPER, user, folder, command, bash_env)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as given:
        if stdin is not None:
            given.write(stdin.encode("utf-8"))
            given.seek(0)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=given if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            env=bash_env,
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


class FileNotFound(Exception):
    """The path names no regular file in the agent's folder."""


def resolve_workspace_path(path: str) -> Optional[str]:
    """``path`` normalized inside an agent's folder, or None when it leaves it."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return None
    if path.startswith("/"):
        return None
    normalized = posixpath.normpath(path)
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


async def read_file(path: str, *, folder: str, user: Optional[str] = None) -> bytes:
    """Read ``path`` in ``folder`` as ``user``; raise ``FileNotFound`` when absent."""
    bash_env = build_bash_env(user, folder)
    proc = await asyncio.create_subprocess_exec(
        *_agent_argv(_READ_FILE, user, folder, path, bash_env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=bash_env,
    )
    data, err = await asyncio.wait_for(proc.communicate(), _FILE_TIMEOUT)
    if proc.returncode == _NOT_A_FILE:
        raise FileNotFound(path)
    if proc.returncode != 0:
        raise OSError(err.decode("utf-8", errors="replace").strip() or "read failed")
    return data


async def write_file(
    path: str, data: bytes, *, folder: str, user: Optional[str] = None
) -> None:
    """Write ``data`` to ``path`` in ``folder`` as ``user``, creating parent folders."""
    bash_env = build_bash_env(user, folder)
    proc = await asyncio.create_subprocess_exec(
        *_agent_argv(_WRITE_FILE, user, folder, path, bash_env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=bash_env,
    )
    _, err = await asyncio.wait_for(proc.communicate(data), _FILE_TIMEOUT)
    if proc.returncode != 0:
        raise OSError(err.decode("utf-8", errors="replace").strip() or "write failed")


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
    """The app of one user's sandbox: ``/health``, ``/exec``, ``/files``, ``/ports``."""

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
                Route("/files/{agent_id}/{path:path}", self.files, methods=["GET", "PUT"]),
                Route(
                    "/ports/{port:int}/{path:path}",
                    self.ports,
                    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                ),
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
        if not _valid_agent_id(agent_id):
            return _agent_id_error()
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return _json_error(400, "INVALID_REQUEST", "command is required")
        timeout = payload.get("timeout", BASH_DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return _json_error(400, "INVALID_REQUEST", "timeout must be a positive number")
        stdin = payload.get("stdin")
        if stdin is not None and not isinstance(stdin, str):
            return _json_error(400, "INVALID_REQUEST", "stdin must be a string")
        env = payload.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(name, str) and _EXTRA_ENV_NAME.match(name) and isinstance(value, str)
            for name, value in env.items()
        ):
            return _json_error(
                400, "INVALID_REQUEST", "env must map MASH_* names to string values"
            )

        result = await run_command(
            command,
            folder=os.path.join(self.workspaces_root, agent_id),
            timeout=float(timeout),
            user=self.user,
            stdin=stdin,
            env=env,
        )
        return JSONResponse(
            {
                "output": result.output,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
            }
        )


    async def files(self, request: Request) -> Response:
        agent_id = request.path_params["agent_id"]
        if not _valid_agent_id(agent_id):
            return _agent_id_error()
        path = resolve_workspace_path(request.path_params["path"])
        if path is None:
            return _json_error(
                400, "INVALID_REQUEST", "path must be relative and stay in the agent's folder"
            )
        folder = os.path.join(self.workspaces_root, agent_id)
        if request.method == "PUT":
            data = await request.body()
            if len(data) > MAX_FILE_BYTES:
                return _json_error(
                    413, "FILE_TOO_LARGE", f"files are limited to {MAX_FILE_BYTES} bytes"
                )
            try:
                await write_file(path, data, folder=folder, user=self.user)
            except (OSError, asyncio.TimeoutError) as exc:
                return _json_error(500, "WRITE_FAILED", str(exc) or "write timed out")
            return JSONResponse({"path": path, "bytes": len(data)})
        try:
            data = await read_file(path, folder=folder, user=self.user)
        except FileNotFound:
            return _json_error(404, "FILE_NOT_FOUND", f"no file at '{path}'")
        except (OSError, asyncio.TimeoutError) as exc:
            return _json_error(500, "READ_FAILED", str(exc) or "read timed out")
        if len(data) > MAX_FILE_BYTES:
            return _json_error(
                413, "FILE_TOO_LARGE", f"files are limited to {MAX_FILE_BYTES} bytes"
            )
        return Response(data, media_type="application/octet-stream")

    async def ports(self, request: Request) -> Response:
        port = int(request.path_params["port"])
        if not 1 <= port <= 65535 or port == SANDBOX_PORT:
            return _json_error(400, "INVALID_REQUEST", f"port {port} cannot be proxied")
        url = f"http://127.0.0.1:{port}/{request.path_params['path']}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_HEADERS
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                upstream = await client.request(
                    request.method, url, headers=headers, content=await request.body()
                )
        except httpx.HTTPError as exc:
            return _json_error(
                502, "PORT_UNREACHABLE", f"nothing answered on port {port}: {exc}"
            )
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() not in _HOP_HEADERS
            },
        )


def _valid_agent_id(agent_id: Any) -> bool:
    return isinstance(agent_id, str) and bool(_AGENT_ID.match(agent_id))


def _agent_id_error() -> JSONResponse:
    return _json_error(
        400,
        "INVALID_REQUEST",
        "agent_id must be 1-128 characters of letters, digits, '.', '_' or '-', "
        "starting with a letter or digit",
    )


def resolve_sandbox_token(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_SANDBOX_TOKEN_ENV) or "").strip()
    return value or None


__all__ = [
    "AGENT_USER",
    "BASH_DEFAULT_TIMEOUT",
    "BASH_MAX_OUTPUT_CHARS",
    "ExecResult",
    "FileNotFound",
    "MASH_SANDBOX_TOKEN_ENV",
    "MAX_FILE_BYTES",
    "SANDBOX_PORT",
    "SandboxDaemon",
    "WORKSPACES_ROOT",
    "build_bash_env",
    "read_file",
    "resolve_bash_user",
    "resolve_sandbox_token",
    "resolve_workspace_path",
    "run_command",
    "truncate_output",
    "write_file",
]

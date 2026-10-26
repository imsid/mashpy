"""Sandbox tools: run code in the request user's sandbox and move files to it.

``run_code`` runs Python, JavaScript or shell code in the agent's folder of the
sandbox. ``sandbox_put`` copies a file from the machine the agent process runs
on into that folder, and ``sandbox_get`` copies one back. The tools run in the
agent's runtime and reach the sandbox only through the host's relay
(``{host}/sandbox``), with the agent's token and the current request id; the
host picks the user from the request.

``sandbox_put`` and ``sandbox_get`` exist only for an agent whose spec declares
file roots (``AgentSpec.sandbox_file_roots()``), and they read and write only
regular files inside those roots. The model chooses the paths, and code in the
sandbox can reach the internet, so a path outside the roots would let it copy
out anything the agent process can read.

An agent whose spec declares sandbox variables (``AgentSpec.sandbox_env()``)
sends them with every ``run_code`` call, so its code, and no other agent's,
sees them, e.g. a read-only database URL.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import httpx

from mash.logging.trace_context import get_request_id
from mash.sandbox.daemon import (
    BASH_MAX_OUTPUT_CHARS,
    BASH_MAX_OUTPUT_LINES,
    MAX_FILE_BYTES,
    WORKSPACES_ROOT,
)

from .base import ToolResult

RUN_CODE_DEFAULT_TIMEOUT = 60
# Room on top of the code's own timeout: a first call starts the sandbox.
_RELAY_OVERHEAD_SECONDS = 180.0
_FILE_RELAY_TIMEOUT = 600.0
# language -> (file extension, interpreter)
_LANGUAGES = {
    "python": ("py", "python3"),
    "javascript": ("js", "node"),
    "shell": ("sh", "bash"),
}
SANDBOX_TOOL_NAMES = ("run_code", "sandbox_put", "sandbox_get")
# The names the sandbox daemon accepts for a command's extra variables.
SANDBOX_ENV_NAME = re.compile(r"^MASH_[A-Z0-9_]{1,64}$")
# Set by the host on every command; an agent's variables cannot replace them.
RESERVED_SANDBOX_ENV = ("MASH_FILES_URL", "MASH_PORTS_URL")
# Kernel and device pseudo-filesystems, refused even inside a root: they expose
# the process's environment and memory as files.
_PSEUDO_FS = (Path("/proc"), Path("/sys"), Path("/dev"))


class _Relay:
    """The host's sandbox relay, as one agent reaches it."""

    def __init__(self, host_api_url: str, token: str) -> None:
        if not str(host_api_url or "").strip():
            raise ValueError("host API URL is required")
        if not str(token or "").strip():
            raise ValueError("agent token is required")
        self.base_url = f"{host_api_url.rstrip('/')}/sandbox"
        self.headers = {"Authorization": f"Bearer {token}"}


def _request_id() -> str | None:
    return get_request_id() or None


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or response.text)
    except ValueError:
        return response.text or f"status {response.status_code}"


class FileRoots:
    """The folders on the agent's machine that the sandbox file tools may use."""

    def __init__(self, roots: Sequence[Path | str]) -> None:
        resolved: list[Path] = []
        for root in roots:
            path = Path(root).expanduser()
            if not path.is_absolute():
                raise ValueError(f"sandbox file root {root} must be an absolute path")
            path = path.resolve()
            if _in_pseudo_fs(path):
                raise ValueError(f"sandbox file root {root} is in {path.parts[1]}, which is refused")
            resolved.append(path)
        if not resolved:
            raise ValueError("at least one sandbox file root is required")
        self.paths = tuple(resolved)

    def describe(self) -> str:
        return ", ".join(str(path) for path in self.paths)

    def resolve(self, raw: str) -> Path:
        """The real path of ``raw`` if it lies inside a root; ValueError otherwise.

        A relative path is taken from the first root. Symlinks are followed, so
        a link inside a root that points out of it is refused.
        """
        path = Path(raw)
        if not path.is_absolute():
            path = self.paths[0] / path
        real = path.resolve()
        if _in_pseudo_fs(real) or not any(real.is_relative_to(root) for root in self.paths):
            raise ValueError(f"{raw} is outside the folders you can use: {self.describe()}")
        return real


def _in_pseudo_fs(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in _PSEUDO_FS)


@dataclass(frozen=True)
class SandboxVariable:
    """A variable an agent's code sees in the sandbox, and what it is for the model."""

    value: str
    description: str


def check_sandbox_env(env: Mapping[str, SandboxVariable]) -> dict[str, SandboxVariable]:
    """``env`` when every name is one the sandbox accepts; ValueError otherwise."""
    checked: dict[str, SandboxVariable] = {}
    for name, variable in env.items():
        if not isinstance(name, str) or not SANDBOX_ENV_NAME.match(name):
            raise ValueError(f"sandbox variable {name!r} must match {SANDBOX_ENV_NAME.pattern}")
        if name in RESERVED_SANDBOX_ENV:
            raise ValueError(f"sandbox variable {name!r} is set by the host")
        if not isinstance(variable, SandboxVariable) or not isinstance(variable.value, str):
            raise ValueError(f"sandbox variable {name!r} must be a SandboxVariable with a str value")
        checked[name] = variable
    return checked


class RunCodeTool:
    """Python, JavaScript or shell in the request user's sandbox."""

    def __init__(
        self,
        *,
        host_api_url: str,
        token: str,
        agent_id: str,
        file_roots: FileRoots | None = None,
        env: Mapping[str, SandboxVariable] | None = None,
    ) -> None:
        self._relay = _Relay(host_api_url, token)
        self._env = {name: variable.value for name, variable in (env or {}).items()}
        folder = f"{WORKSPACES_ROOT}/{agent_id}"
        self.name = "run_code"
        self.description = (
            "Run code on your computer: a Linux sandbox that belongs to the user you "
            "are working for. Use it to analyse data, build files, pages and charts, "
            "keep databases, and run servers.\n"
            "\n"
            "How it runs:\n"
            f"- Each call writes `code` to a file and runs it in your folder, {folder}, "
            "as a new process. Files in the folder persist across calls and requests "
            "for this user; variables and shell state do not, so keep data between "
            "calls in files.\n"
            "- language `python` runs python3, `javascript` runs node, `shell` runs bash.\n"
            f"- Output is stdout and stderr together, cut to {BASH_MAX_OUTPUT_LINES} "
            f"lines and {BASH_MAX_OUTPUT_CHARS} characters. Write large results to a "
            "file and print a summary.\n"
            "- A non-zero exit returns the output and exit code: read the error, fix "
            "the code, run it again.\n"
            "\n"
            "What is installed:\n"
            "- Python 3.12 with pip and uv. Only the standard library is installed: "
            "install pandas, matplotlib or anything else you need first (see below).\n"
            "- Node 22 with npm.\n"
            "- SQLite: the sqlite3 CLI and Python's sqlite3 module. A database is a "
            "file in your folder, e.g. data.db.\n"
            "- git, curl, jq, rg. The sandbox can reach the internet.\n"
            "- Packages: `pip install --user <pkg>` or `npm install <pkg>`, run as "
            "shell. Both install into your folder and persist, so install once and "
            "check with `pip list --user` before installing again.\n"
            "\n"
            "Showing results to the user:\n"
            "- A file at <path> in your folder opens at \"$MASH_FILES_URL/<path>\". "
            "To hand the user an HTML page, chart or CSV, write it to the folder and "
            "print that URL from your code (e.g. `echo \"$MASH_FILES_URL/report.html\"` "
            "or `os.environ['MASH_FILES_URL']`), then give the user the printed link. "
            "Never guess the URL.\n"
            "- A server started in the background on port <n> opens at "
            "\"$MASH_PORTS_URL/<n>/\". Start it detached, e.g. "
            "`nohup python3 -m http.server 8050 >server.log 2>&1 &`, and use relative "
            "URLs in the pages it serves. Servers stop when the sandbox goes idle; "
            "prefer files for pages that need no live server.\n"
            "\n"
            + (
                "Files from elsewhere: copy files in with sandbox_put, from "
                f"{file_roots.describe()} on the agent's machine. "
                if file_roots is not None
                else "Files from elsewhere: write data from tool results into your code. "
            )
            + _describe_env(env or {})
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": list(_LANGUAGES),
                    "description": "The language of `code`.",
                },
                "code": {"type": "string", "description": "The code to run."},
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default: {RUN_CODE_DEFAULT_TIMEOUT})",
                    "default": RUN_CODE_DEFAULT_TIMEOUT,
                },
            },
            "required": ["language", "code"],
        }
        self.requires_approval = False
        self.parallel_safe = False

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        language = args.get("language")
        if language not in _LANGUAGES:
            return ToolResult.error(f"language must be one of: {', '.join(_LANGUAGES)}")
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolResult.error("No code provided")
        timeout = args.get("timeout", RUN_CODE_DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return ToolResult.error("timeout must be a positive number of seconds")
        request_id = _request_id()
        if not request_id:
            return ToolResult.error("run_code runs only within a request")

        extension, interpreter = _LANGUAGES[language]
        # The code arrives on stdin and is written to a file as the agent user,
        # so it needs no quoting and errors name a real file and line. The code
        # itself gets /dev/null as stdin, as a command would.
        script = f".mash/runs/{uuid.uuid4().hex}.{extension}"
        command = (
            f'mkdir -p .mash/runs && cat > {script} && '
            f'{{ {interpreter} {script} </dev/null; rc=$?; rm -f {script}; exit $rc; }}'
        )
        try:
            async with httpx.AsyncClient(
                timeout=float(timeout) + _RELAY_OVERHEAD_SECONDS
            ) as client:
                response = await client.post(
                    f"{self._relay.base_url}/exec",
                    json={
                        "request_id": request_id,
                        "command": command,
                        "stdin": code,
                        "timeout": timeout,
                        **({"env": self._env} if self._env else {}),
                    },
                    headers=self._relay.headers,
                )
        except httpx.HTTPError as exc:
            return ToolResult.error(f"Error reaching the sandbox: {exc}")

        if response.status_code != 200:
            return ToolResult.error(f"Sandbox error: {_error_message(response)}")
        result = response.json()
        if result.get("timed_out"):
            return ToolResult.error(f"Code timed out after {timeout} seconds")
        output = str(result.get("output") or "")
        exit_code = result.get("exit_code")
        if exit_code == 127 and f"{interpreter}: command not found" in output:
            return ToolResult.error(f"{language} is not installed in this sandbox image")
        if exit_code != 0:
            return ToolResult(
                content=output,
                is_error=False,  # The model reads the error and fixes the code.
                metadata={"exit_code": exit_code},
            )
        return ToolResult.success(output, exit_code=exit_code)

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _describe_env(env: Mapping[str, SandboxVariable]) -> str:
    if not env:
        return "The sandbox holds no secrets or credentials."
    lines = "".join(
        f"- {name}: {variable.description}\n" for name, variable in env.items()
    )
    return (
        "\n\nYour code also sees these variables. Read them from the environment; "
        "never print them or write their values into files, which links can serve:\n"
        + lines
    )


class SandboxPutTool:
    """Copy a file from the agent's machine into the sandbox folder."""

    def __init__(self, *, host_api_url: str, token: str, file_roots: FileRoots) -> None:
        self._relay = _Relay(host_api_url, token)
        self._roots = file_roots
        self.name = "sandbox_put"
        self.description = (
            "Copy a file from the machine you run on into your sandbox folder, so "
            "run_code can use it. You can copy files from these folders only: "
            f"{file_roots.describe()}. Use it for files your other tools wrote "
            "there. For small data from a tool result, write it into your code "
            "instead.\n"
            f"Files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "src": {
                    "type": "string",
                    "description": "Path of the file on your machine, absolute or "
                    f"relative to {file_roots.paths[0]}.",
                },
                "dest": {
                    "type": "string",
                    "description": "Path in your sandbox folder, relative to it, "
                    "e.g. data/sales.csv. Parent folders are created.",
                },
            },
            "required": ["src", "dest"],
        }
        self.requires_approval = False
        self.parallel_safe = False

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        src = str(args.get("src") or "").strip()
        dest = str(args.get("dest") or "").strip()
        if not src or not dest:
            return ToolResult.error("src and dest are required")
        request_id = _request_id()
        if not request_id:
            return ToolResult.error("sandbox_put runs only within a request")
        try:
            path = self._roots.resolve(src)
            data = await asyncio.to_thread(_read_local, path)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"Cannot read {src}: {exc.strerror or exc}")
        try:
            async with httpx.AsyncClient(timeout=_FILE_RELAY_TIMEOUT) as client:
                response = await client.put(
                    f"{self._relay.base_url}/files",
                    params={"request_id": request_id, "path": dest},
                    content=data,
                    headers=self._relay.headers,
                )
        except httpx.HTTPError as exc:
            return ToolResult.error(f"Error reaching the sandbox: {exc}")
        if response.status_code != 200:
            return ToolResult.error(f"Sandbox error: {_error_message(response)}")
        written = response.json()
        return ToolResult.success(
            f"Copied {written.get('bytes', len(data))} bytes to {written.get('path', dest)} "
            "in your sandbox folder."
        )

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class SandboxGetTool:
    """Copy a file from the sandbox folder to the agent's machine."""

    def __init__(self, *, host_api_url: str, token: str, file_roots: FileRoots) -> None:
        self._relay = _Relay(host_api_url, token)
        self._roots = file_roots
        self.name = "sandbox_get"
        self.description = (
            "Copy a file from your sandbox folder to the machine you run on, for "
            "example a result your other tools need to read. You can write into "
            f"these folders only: {file_roots.describe()}. To show a file to the "
            "user, give them its link from run_code instead.\n"
            f"Files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "src": {
                    "type": "string",
                    "description": "Path in your sandbox folder, relative to it.",
                },
                "dest": {
                    "type": "string",
                    "description": "Path on your machine to write, absolute or "
                    f"relative to {file_roots.paths[0]}. Parent folders are "
                    "created. An existing file is replaced.",
                },
            },
            "required": ["src", "dest"],
        }
        self.requires_approval = False
        self.parallel_safe = False

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        src = str(args.get("src") or "").strip()
        dest = str(args.get("dest") or "").strip()
        if not src or not dest:
            return ToolResult.error("src and dest are required")
        request_id = _request_id()
        if not request_id:
            return ToolResult.error("sandbox_get runs only within a request")
        try:
            path = self._roots.resolve(dest)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        try:
            async with httpx.AsyncClient(timeout=_FILE_RELAY_TIMEOUT) as client:
                response = await client.get(
                    f"{self._relay.base_url}/files",
                    params={"request_id": request_id, "path": src},
                    headers=self._relay.headers,
                )
        except httpx.HTTPError as exc:
            return ToolResult.error(f"Error reaching the sandbox: {exc}")
        if response.status_code != 200:
            return ToolResult.error(f"Sandbox error: {_error_message(response)}")
        try:
            await asyncio.to_thread(_write_local, self._roots, path, response.content)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        except OSError as exc:
            return ToolResult.error(f"Cannot write {path}: {exc.strerror or exc}")
        return ToolResult.success(f"Copied {len(response.content)} bytes to {path}.")

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _read_local(path: Path) -> bytes:
    # O_NOFOLLOW refuses a symlink swapped in after the path was resolved, and
    # fstat checks the file that was actually opened.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path} is {info.st_size} bytes; the limit is {MAX_FILE_BYTES}")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _write_local(roots: FileRoots, path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The folders just made must not lead out of the roots through a symlink.
    roots.resolve(str(path))
    # O_NONBLOCK keeps a FIFO planted at the path from blocking the open.
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        os.set_blocking(fd, True)
        os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def build_sandbox_tools(
    *,
    host_api_url: str,
    token: str,
    agent_id: str,
    file_roots: Sequence[Path | str] = (),
    env: Mapping[str, SandboxVariable] | None = None,
) -> list[Any]:
    """The sandbox tools for one agent.

    ``sandbox_put`` and ``sandbox_get`` come only with ``file_roots``. ``env``
    names the variables ``run_code`` passes to the agent's code.
    """
    roots = FileRoots(file_roots) if file_roots else None
    tools: list[Any] = [
        RunCodeTool(
            host_api_url=host_api_url,
            token=token,
            agent_id=agent_id,
            file_roots=roots,
            env=check_sandbox_env(env or {}),
        )
    ]
    if roots is not None:
        tools.append(SandboxPutTool(host_api_url=host_api_url, token=token, file_roots=roots))
        tools.append(SandboxGetTool(host_api_url=host_api_url, token=token, file_roots=roots))
    return tools


__all__ = [
    "RESERVED_SANDBOX_ENV",
    "RUN_CODE_DEFAULT_TIMEOUT",
    "SANDBOX_ENV_NAME",
    "SANDBOX_TOOL_NAMES",
    "FileRoots",
    "RunCodeTool",
    "SandboxGetTool",
    "SandboxPutTool",
    "SandboxVariable",
    "build_sandbox_tools",
    "check_sandbox_env",
]

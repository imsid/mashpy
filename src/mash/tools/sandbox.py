"""Sandbox tools: run code in the request user's sandbox and move files to it.

``run_code`` runs Python, JavaScript or shell code in the agent's folder of the
sandbox. ``sandbox_put`` copies a file from the machine the agent process runs
on into that folder, and ``sandbox_get`` copies one back. The tools run in the
agent's runtime and reach the sandbox only through the host's relay
(``{host}/sandbox``), with the agent's token and the current request id; the
host picks the user from the request.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Dict

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


class RunCodeTool:
    """Python, JavaScript or shell in the request user's sandbox."""

    def __init__(self, *, host_api_url: str, token: str, agent_id: str) -> None:
        self._relay = _Relay(host_api_url, token)
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
            "Files from elsewhere: copy files from the agent's machine in with "
            "sandbox_put. The sandbox holds no secrets or credentials."
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
        # so it needs no quoting and errors name a real file and line.
        script = f".mash/runs/{uuid.uuid4().hex}.{extension}"
        command = (
            f'mkdir -p .mash/runs && cat > {script} && '
            f'{{ {interpreter} {script}; rc=$?; rm -f {script}; exit $rc; }}'
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


class SandboxPutTool:
    """Copy a file from the agent's machine into the sandbox folder."""

    def __init__(self, *, host_api_url: str, token: str) -> None:
        self._relay = _Relay(host_api_url, token)
        self.name = "sandbox_put"
        self.description = (
            "Copy a file from the machine you run on into your sandbox folder, so "
            "run_code can use it. Use it for files the user points you to and files "
            "your other tools wrote. For small data from a tool result, write it into "
            "your code instead.\n"
            "Never copy .env files, keys, tokens, credentials, password stores or "
            "any other sensitive file into the sandbox: code there can reach the "
            "internet. Copy only the data the task needs.\n"
            f"Files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "src": {
                    "type": "string",
                    "description": "Path of the file on your machine; ~ is expanded.",
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
        path = Path(os.path.expanduser(src))
        if not path.is_file():
            return ToolResult.error(f"no file at {path}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return ToolResult.error(f"{path} is {size} bytes; the limit is {MAX_FILE_BYTES}")
        data = await asyncio.to_thread(path.read_bytes)
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

    def __init__(self, *, host_api_url: str, token: str) -> None:
        self._relay = _Relay(host_api_url, token)
        self.name = "sandbox_get"
        self.description = (
            "Copy a file from your sandbox folder to the machine you run on, for "
            "example a result your other tools need to read. To show a file to the "
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
                    "description": "Path on your machine to write; ~ is expanded and "
                    "parent folders are created. An existing file is replaced.",
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
        path = Path(os.path.expanduser(dest))
        try:
            await asyncio.to_thread(_write_local, path, response.content)
        except OSError as exc:
            return ToolResult.error(f"Cannot write {path}: {exc}")
        return ToolResult.success(f"Copied {len(response.content)} bytes to {path}.")

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _write_local(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_sandbox_tools(*, host_api_url: str, token: str, agent_id: str) -> list[Any]:
    """The sandbox tools for one agent."""
    return [
        RunCodeTool(host_api_url=host_api_url, token=token, agent_id=agent_id),
        SandboxPutTool(host_api_url=host_api_url, token=token),
        SandboxGetTool(host_api_url=host_api_url, token=token),
    ]


__all__ = [
    "RUN_CODE_DEFAULT_TIMEOUT",
    "SANDBOX_TOOL_NAMES",
    "RunCodeTool",
    "SandboxGetTool",
    "SandboxPutTool",
    "build_sandbox_tools",
]

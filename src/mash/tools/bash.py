"""Bash tool: runs the model's commands in the request user's sandbox.

The tool runs in the agent's runtime and starts no shell itself. Each call
posts the command to the host's sandbox relay (``{host}/sandbox/exec``) with
the agent's token and the current request id; the host runs it in the sandbox
of the user the request acts for, in this agent's folder there.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

import httpx

from mash.logging.trace_context import get_request_id
from mash.sandbox.daemon import BASH_DEFAULT_TIMEOUT

from .base import ToolResult

_ROOT_FIND_RE = re.compile(r"(^|[;&|()]\s*)find\s+/($|\s)")
# Room on top of the command's own timeout: a first call starts the sandbox.
_RELAY_OVERHEAD_SECONDS = 180.0


def _matches_root_find(command: str) -> bool:
    """Detect full-disk scans like `find / ...`."""
    return bool(_ROOT_FIND_RE.search(command))


class BashTool:
    """Bash in the request user's sandbox, reached through the host."""

    def __init__(self, *, host_api_url: str, token: str) -> None:
        """Initialize bash tool.

        Args:
            host_api_url: The host's base URL, as the agent reaches it.
            token: The agent's token.
        """
        if not str(host_api_url or "").strip():
            raise ValueError("host API URL is required")
        if not str(token or "").strip():
            raise ValueError("agent token is required")
        self._exec_url = f"{host_api_url.rstrip('/')}/sandbox/exec"
        self._token = token
        self.name = "bash"
        self.description = (
            "Execute a bash command in your workspace.\n"
            "Each call starts a fresh shell in your workspace folder; files you "
            "write there persist between calls, shell state (cd, variables) does not.\n"
            "Rules:\n"
            "- Prefer ripgrep (rg) for search.\n"
            "- Keep outputs small using head, tail, or context flags (-A / -C).\n"
            "- Avoid full-file dumps and recursive/unbounded scans.\n"
            "- Never run full-disk scans like `find / ...`.\n"
            "- Ignore irrelevant directories unless asked: node_modules, __pycache__, "
            ".git, venv, dist, build, migrations, tests.\n"
            "- Truncate outputs to ~50 lines or ~4000 chars.\n"
            "Examples:\n"
            "- rg -l \"payment\" --type py\n"
            "- rg \"def process_payment\" -A 12\n"
            "- tree -L 2 -I 'node_modules|__pycache__|.git'\n"
            "- sed -n '1,120p' src/app.py"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                    "default": BASH_DEFAULT_TIMEOUT,
                },
            },
            "required": ["command"],
        }
        self.requires_approval = False
        self.parallel_safe = False

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Run the command in the sandbox.

        Args:
            args: Tool arguments containing 'command' and optional 'timeout'.

        Returns:
            ToolResult with command output.
        """
        command = args.get("command", "")
        if not command:
            return ToolResult.error("No command provided")

        is_valid, error_msg = self._validate_command(command)
        if not is_valid:
            return ToolResult.error(f"Invalid command: {error_msg}")

        request_id = get_request_id()
        if not request_id:
            return ToolResult.error("bash runs only within a request")

        timeout = args.get("timeout", BASH_DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return ToolResult.error("timeout must be a positive number of seconds")
        try:
            async with httpx.AsyncClient(
                timeout=float(timeout) + _RELAY_OVERHEAD_SECONDS
            ) as client:
                response = await client.post(
                    self._exec_url,
                    json={"request_id": request_id, "command": command, "timeout": timeout},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as exc:
            return ToolResult.error(f"Error reaching the sandbox: {exc}")

        if response.status_code != 200:
            return ToolResult.error(f"Sandbox error: {_error_message(response)}")
        result = response.json()
        if result.get("timed_out"):
            return ToolResult.error(f"Command timed out after {timeout} seconds")
        output = str(result.get("output") or "")
        exit_code = result.get("exit_code")
        if exit_code != 0:
            return ToolResult(
                content=output,
                is_error=False,  # Non-zero exit is not necessarily an error
                metadata={"exit_code": exit_code},
            )
        return ToolResult.success(output, exit_code=exit_code)

    def _validate_command(self, command: str) -> tuple[bool, Optional[str]]:
        """Validate that the command is safe to execute.

        Args:
            command: Command to validate.

        Returns:
            Tuple of (is_valid, error_message).
        """
        dangerous_patterns = [
            "rm -rf /",
            "mkfs",
            ":(){:|:&};:",
            "shutdown",
            "reboot",
            "sudo",
        ]

        for pattern in dangerous_patterns:
            if pattern in command:
                return False, f"Command contains dangerous pattern: {pattern}"

        if _matches_root_find(command):
            return False, "Avoid full-disk scans (find / ...); scope to the repo or a subdirectory."

        return True, None

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or response.text)
    except ValueError:
        return response.text or f"status {response.status_code}"


__all__ = ["BashTool"]

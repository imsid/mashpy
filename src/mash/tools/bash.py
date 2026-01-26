"""Bash tool for executing shell commands."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import ToolResult

BASH_DEFAULT_TIMEOUT = 30
BASH_MAX_OUTPUT_LINES = 50  # Reduced from 100 to prevent token explosion

_BASH_SENTINEL_PREFIX = "__mash_bash_done__"
_BASH_EXIT_PREFIX = "__mash_bash_exit__"


class BashSession:
    """Persistent bash session for executing commands."""

    def __init__(self, working_dir: Optional[str] = None) -> None:
        """Initialize bash session.

        Args:
            working_dir: Working directory for the session.
        """
        self.working_dir = working_dir
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._start_process()

    def _start_process(self) -> None:
        """Start the bash process."""
        self._process = subprocess.Popen(
            ["/bin/bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.working_dir,
        )
        self._stdout_queue = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            name="bash-session-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        """Read stdout in a separate thread."""
        assert self._process is not None
        if self._process.stdout is None:
            return
        for line in self._process.stdout:
            self._stdout_queue.put(line)

    def restart(self, working_dir: Optional[str] = None) -> None:
        """Restart the session with a new working directory."""
        self.shutdown()
        self.working_dir = working_dir
        self._start_process()

    def shutdown(self) -> None:
        """Shut down the bash session."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
        self._process = None

    def execute_command(
        self, command: str, timeout: int = BASH_DEFAULT_TIMEOUT
    ) -> tuple[str, int]:
        """Execute a command in the bash session.

        Args:
            command: Command to execute.
            timeout: Timeout in seconds.

        Returns:
            Tuple of (output, exit_code).

        Raises:
            RuntimeError: If the session is not running.
            TimeoutError: If the command times out.
        """
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Bash session is not running.")

        token = uuid.uuid4().hex
        sentinel = f"{_BASH_SENTINEL_PREFIX}{token}"
        exit_marker = f"{_BASH_EXIT_PREFIX}{token}"

        payload = f"{command}\n" f'echo "{exit_marker}$?"\n' f'echo "{sentinel}"\n'

        self._process.stdin.write(payload)
        self._process.stdin.flush()

        output_lines, exit_code, total_lines = self._read_until_sentinel(
            sentinel, exit_marker, timeout
        )
        output_text = self._truncate_output(output_lines, total_lines)

        return output_text, exit_code

    def _read_until_sentinel(
        self,
        sentinel: str,
        exit_marker: str,
        timeout: int,
    ) -> tuple[List[str], int, int]:
        """Read output until the sentinel is found."""
        lines: List[str] = []
        exit_code = 0
        start = time.time()
        total_lines = 0

        while True:
            remaining = timeout - int(time.time() - start)
            if remaining <= 0:
                raise TimeoutError("Command timed out")

            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Command timed out") from exc

            stripped = line.rstrip("\n")

            if stripped.startswith(exit_marker):
                raw = stripped[len(exit_marker) :].strip()
                try:
                    exit_code = int(raw)
                except ValueError:
                    exit_code = 0
                continue

            if stripped == sentinel:
                return lines, exit_code, total_lines

            total_lines += 1
            if total_lines <= BASH_MAX_OUTPUT_LINES:
                lines.append(line.rstrip("\n"))

    def _truncate_output(self, lines: List[str], total_lines: int) -> str:
        """Truncate output if too many lines."""
        if not lines:
            return ""

        if total_lines <= BASH_MAX_OUTPUT_LINES:
            return "\n".join(lines)

        truncated = "\n".join(lines)
        return f"{truncated}\n\n... Output truncated ({total_lines} total lines) ..."


class BashTool:
    """Bash tool for executing shell commands."""

    def __init__(self, working_dir: Optional[str] = None) -> None:
        """Initialize bash tool.

        Args:
            working_dir: Working directory for bash commands.
        """
        self.name = "bash"
        self.description = "Execute bash commands in a persistent session"
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
        self._session = BashSession(working_dir=working_dir)

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the bash command.

        Args:
            args: Tool arguments containing 'command' and optional 'timeout'.

        Returns:
            ToolResult with command output.
        """
        command = args.get("command", "")
        if not command:
            return ToolResult.error("No command provided")

        # Validate command
        is_valid, error_msg = self._validate_command(command)
        if not is_valid:
            return ToolResult.error(f"Invalid command: {error_msg}")

        # Execute command
        timeout = args.get("timeout", BASH_DEFAULT_TIMEOUT)
        try:
            output, exit_code = self._session.execute_command(command, timeout)
            if exit_code != 0:
                return ToolResult(
                    content=output,
                    is_error=False,  # Non-zero exit is not necessarily an error
                    metadata={"exit_code": exit_code},
                )
            return ToolResult.success(output, exit_code=exit_code)
        except TimeoutError as e:
            return ToolResult.error(
                f"Command timed out after {timeout} seconds: {str(e)}"
            )
        except Exception as e:
            return ToolResult.error(f"Error executing command: {str(e)}")

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

        return True, None

    def restart(self, working_dir: Optional[str] = None) -> None:
        """Restart the bash session with a new working directory.

        Args:
            working_dir: New working directory.
        """
        self._session.restart(working_dir)

    def shutdown(self) -> None:
        """Shut down the bash session."""
        self._session.shutdown()

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

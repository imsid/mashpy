"""Bash session support for the Claude bash tool."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
import uuid
from typing import List, Optional

BASH_TOOL_NAME = "bash"
BASH_TOOL_TYPE = "bash_20250124"
BASH_DEFAULT_TIMEOUT_SECONDS = 30
BASH_MAX_OUTPUT_LINES = 100

_BASH_SENTINEL_PREFIX = "__mash_bash_done__"
_BASH_EXIT_PREFIX = "__mash_bash_exit__"


class BashSession:
    """Persistent bash session used by the Claude bash tool."""

    def __init__(self, working_dir: Optional[str]) -> None:
        self.working_dir = working_dir
        self._process: Optional["subprocess.Popen[str]"] = None
        self._stdout_queue: "queue.Queue[str]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._start_process()

    def _start_process(self) -> None:
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
            target=self._read_stdout, name="bash-session-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        assert self._process is not None
        if self._process.stdout is None:
            return
        for line in self._process.stdout:
            self._stdout_queue.put(line)

    def restart(self, working_dir: Optional[str]) -> None:
        self.shutdown()
        self.working_dir = working_dir
        self._start_process()

    def shutdown(self) -> None:
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

    def execute_command(self, command: str, timeout: int) -> tuple[str, int]:
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
        lines: List[str] = []
        exit_code: int = 0
        start = time.time()
        total_lines = 0
        while True:
            remaining = timeout - int(time.time() - start)
            if remaining <= 0:
                raise TimeoutError("command timed out")
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("command timed out") from exc
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
        if not lines:
            return ""
        if total_lines <= BASH_MAX_OUTPUT_LINES:
            return "\n".join(lines)
        truncated = "\n".join(lines)
        return f"{truncated}\n\n... Output truncated ({total_lines} total lines) ..."


def validate_bash_command(command: str) -> tuple[bool, Optional[str]]:
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

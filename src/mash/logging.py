"""Structured logging helpers for the CLI framework."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Union


class Logger(Protocol):
    """Protocol describing the logger interface."""

    def debug(self, msg: str, **extra: Any) -> None:
        """Emit a debug-level log entry."""

    def info(self, msg: str, **extra: Any) -> None:
        """Emit an info-level log entry."""

    def warn(self, msg: str, **extra: Any) -> None:
        """Emit a warning-level log entry."""

    def error(self, msg: str, **extra: Any) -> None:
        """Emit an error-level log entry."""


class JsonLogger(Logger):
    """Very small JSON logger that prints one line per entry."""

    def __init__(
        self,
        app_name: str,
        log_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self._app = app_name
        self._log_path = Path(log_path).expanduser() if log_path else None
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def debug(self, msg: str, **extra: Any) -> None:
        """Emit a debug log."""

        self._log("debug", msg, extra)

    def info(self, msg: str, **extra: Any) -> None:
        """Emit an info log."""

        self._log("info", msg, extra)

    def warn(self, msg: str, **extra: Any) -> None:
        """Emit a warning log."""

        self._log("warn", msg, extra)

    def error(self, msg: str, **extra: Any) -> None:
        """Emit an error log."""

        self._log("error", msg, extra)

    def _log(
        self, level: str, msg: str, extra: Optional[Dict[str, Any]]
    ) -> None:
        """Write a JSON line to stderr or a log file."""

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "logger": self._app,
            "level": level,
            "msg": msg,
        }
        if extra:
            payload.update(extra)
        line = json.dumps(payload, default=str)
        if self._log_path:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        else:
            print(line, file=sys.stderr)

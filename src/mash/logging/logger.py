"""File-based event logger with JSON lines output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from .events import LogEvent


class EventLogger:
    """Writes log events as JSON lines to a configured destination.

    Each event is serialized to JSON and appended to the log file as a single line.
    This format is ideal for:
    - Stream processing with tools like jq, grep, etc.
    - Easy parsing and analysis
    - Appending without corrupting the file

    Example:
        >>> logger = EventLogger("~/.mash/logs/events.jsonl")
        >>> event = CommandEvent(
        ...     event_type="command.start",
        ...     app_id="codebase",
        ...     session_id="abc123",
        ...     command_name="/help",
        ... )
        >>> logger.emit(event)
    """

    def __init__(self, destination: Union[str, Path]) -> None:
        """Initialize event logger.

        Args:
            destination: Path to log file. Parent directories will be created
                        if they don't exist. Use "~" for home directory.
        """
        self._destination = Path(destination).expanduser()
        self._destination.parent.mkdir(parents=True, exist_ok=True)

    @property
    def destination(self) -> Path:
        """Get the log file destination path."""
        return self._destination

    def emit(self, event: LogEvent) -> None:
        """Write an event to the log file.

        The event is serialized to JSON and appended as a single line.
        Datetime objects are converted to ISO format strings.

        Args:
            event: Event to log.
        """
        payload = event.to_dict()
        line = json.dumps(payload, default=str)
        with self._destination.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def clear(self) -> None:
        """Clear the log file by truncating it to zero length."""
        if self._destination.exists():
            self._destination.unlink()

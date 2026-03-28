"""MemoryStore-backed structured event logger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .events import LogEvent, normalize_log_event

if TYPE_CHECKING:
    from ..memory.store.protocol import MemoryStore


class EventLogger:
    """Writes structured events into a MemoryStore."""

    def __init__(self, store: "MemoryStore") -> None:
        """Initialize event logger."""
        self._store: Any = store

    @property
    def store(self) -> "MemoryStore":
        """Return the backing MemoryStore."""
        return self._store

    def emit(self, event: LogEvent) -> None:
        """Persist one structured event."""
        self._store.save_logs([normalize_log_event(event)])

    def clear(self) -> None:
        """Structured log clearing is not part of the MemoryStore contract."""
        raise NotImplementedError("clearing structured logs is not supported")

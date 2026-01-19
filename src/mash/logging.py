"""Structured event logging helpers for the CLI framework."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union


@dataclass(frozen=True)
class LogEvent:
    """Base event written to the log destination."""

    event_type: str
    app_id: str
    session_id: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "ts": self.ts,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "event_class": type(self).__name__,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class AgentTraceEvent(LogEvent):
    """Structured event emitted during agent execution."""

    trace_id: Optional[str] = None
    duration_ms: Optional[int] = None
    step_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "trace_id": self.trace_id,
                "step_id": self.step_id,
                "duration_ms": self.duration_ms,
            }
        )
        return payload


@dataclass(frozen=True)
class DebugEvent(LogEvent):
    """Free-form debug event."""


@dataclass(frozen=True)
class CommandEvent(LogEvent):
    """Event emitted for command execution lifecycle."""


class EventLogger:
    """Writes log events as JSON lines to a configured destination."""

    def __init__(self, destination: Union[str, Path]) -> None:
        self._destination = Path(destination).expanduser()
        self._destination.parent.mkdir(parents=True, exist_ok=True)

    @property
    def destination(self) -> Path:
        return self._destination

    def emit(self, event: LogEvent) -> None:
        payload = event.to_dict()
        line = json.dumps(payload, default=str)
        with self._destination.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

"""Structured event logger for canonical runtime storage."""

from __future__ import annotations

from typing import Any

from .events import LogEvent
from .trace_context import get_request_id, get_trace_id
from ..runtime.events import RuntimeEvent

class EventLogger:
    """Writes structured events into a canonical event sink."""

    def __init__(self, store: Any) -> None:
        """Initialize event logger."""
        self._store = store

    @property
    def store(self) -> Any:
        """Return the backing sink."""
        return self._store

    async def emit(self, event: LogEvent) -> None:
        """Persist one structured event."""
        if not hasattr(self._store, "append_event"):
            raise TypeError("event logger sink must support append_event()")
        runtime_store = self._store
        await runtime_store.append_event(self._to_runtime_event(event))

    @staticmethod
    def _to_runtime_event(event: LogEvent) -> RuntimeEvent:
        raw = event.to_dict()
        trace_id = raw.get("trace_id")
        resolved_trace_id = (
            trace_id.strip()
            if isinstance(trace_id, str) and trace_id.strip()
            else get_trace_id()
        )
        payload = {
            key: value
            for key, value in raw.items()
            if key not in {"event_type", "ts", "app_id", "session_id", "trace_id", "event_class"}
            and value is not None
        }
        return RuntimeEvent(
            app_id=str(raw["app_id"]),
            agent_id=str(raw["app_id"]),
            event_type=str(raw["event_type"]),
            request_id=get_request_id(),
            session_id=raw.get("session_id"),
            trace_id=resolved_trace_id,
            payload=payload,
            created_at=float(raw["ts"]),
        )

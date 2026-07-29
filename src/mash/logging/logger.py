"""Structured event logger for canonical runtime storage."""

from __future__ import annotations

from typing import Any

from .events import LogEvent
from .trace_context import (
    get_host_id,
    get_request_id,
    get_session_id,
    get_trace_id,
)
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
        # The ambient trace wins, matching how request_id resolves below.
        # An LLM provider holds its trace id as an instance field and the
        # runtime shares one provider across every request an agent serves,
        # so concurrent requests overwrite each other's value; the event's
        # own trace id is the fallback for emitters outside a request.
        event_trace_id = raw.get("trace_id")
        resolved_trace_id = get_trace_id() or (
            event_trace_id.strip()
            if isinstance(event_trace_id, str) and event_trace_id.strip()
            else None
        )
        # Same reasoning for the session: a provider holds it as an instance
        # field, so two requests in different sessions would otherwise stamp
        # each other's events and skew every session-scoped query.
        resolved_session_id = get_session_id() or raw.get("session_id")
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
            host_id=get_host_id(),
            session_id=resolved_session_id,
            trace_id=resolved_trace_id,
            payload=payload,
            created_at=float(raw["ts"]),
        )

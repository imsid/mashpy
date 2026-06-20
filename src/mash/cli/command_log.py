"""Ship CLI command lifecycle events to the host runtime.

The REPL emits ``command.start`` / ``command.complete`` / ``command.error``
events around each ``/command`` invocation. This logger forwards them to the
deployment so they surface in the admin UI's CLI logs. It is strictly
best-effort: telemetry must never block or break an interactive session.
"""

from __future__ import annotations

import threading
from typing import Optional

from mash.logging.events import CommandEvent

from .client import MashHostClient


class RemoteCommandEventLogger:
    """Best-effort logger that posts CLI command events to the host."""

    def __init__(
        self,
        client: MashHostClient,
        *,
        agent_id: str,
        host_id: Optional[str] = None,
    ) -> None:
        self._client = client
        self._agent_id = agent_id
        self._host_id = host_id

    def emit(self, event: CommandEvent) -> None:
        payload = {
            "agent_id": self._agent_id,
            "host_id": self._host_id,
            "session_id": event.session_id,
            "event_type": event.event_type,
            "command_name": event.command_name,
            "args": event.args,
            "duration_ms": event.duration_ms,
            "error": event.error,
            "trace_id": event.trace_id,
            "ts": event.ts,
        }
        # Fire-and-forget so a slow or unreachable host never stalls the REPL.
        thread = threading.Thread(
            target=self._post, args=(payload,), daemon=True
        )
        thread.start()

    def _post(self, payload: dict) -> None:
        try:
            self._client.record_command_event(payload)
        except Exception:
            # Telemetry is non-essential; swallow any transport/server error.
            pass


__all__ = ["RemoteCommandEventLogger"]

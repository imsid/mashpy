"""Events, the sessions that order them, and their ids."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# request_id = uuid5(EVENTS_NS, "<session_id>:<event_id>"): an event's request
# is known before the event runs, and submitting it again finds the same one.
EVENTS_NS = uuid.UUID("6f1d2c1e-6b54-5d0a-9c2e-8a0f3d6b7e41")

QUEUED = "queued"
STARTED = "started"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
CLOSED = frozenset({COMPLETED, FAILED, CANCELLED})

# What the API reports for a started event: waiting while its request has an
# open interaction, otherwise running.
RUNNING = "running"
WAITING = "waiting"
REPORTED_STATUSES = frozenset({QUEUED, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED})

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


def _validate(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"{name} must be 1-255 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return value


def validate_session_id(session_id: Any) -> str:
    return _validate("session_id", session_id)


def validate_event_id(event_id: Any) -> str:
    return _validate("event_id", event_id)


def event_request_id(session_id: str, event_id: str) -> str:
    """The id of the request an event runs as."""
    return str(uuid.uuid5(EVENTS_NS, f"{session_id}:{event_id}"))


@dataclass(frozen=True)
class EventRecord:
    """One row of the ``events`` ledger."""

    session_id: str
    event_id: str
    seq: int
    status: str
    body: dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    received_at: float = 0.0
    started_at: Optional[float] = None
    response: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    closed_at: Optional[float] = None

    @property
    def app_id(self) -> str:
        return str(self.body.get("app_id") or "")


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    host_id: str
    policy_id: str


class EventNotFoundError(LookupError):
    def __init__(self, session_id: str, event_id: str) -> None:
        super().__init__(f"event '{event_id}' not found in session '{session_id}'")


class SessionMismatchError(RuntimeError):
    """The session is bound to another host or policy (``kind``)."""

    def __init__(self, session_id: str, kind: str, bound: str) -> None:
        super().__init__(f"session '{session_id}' runs on {kind} '{bound}'")
        self.kind = kind


class EventTerminalError(RuntimeError):
    """The event has already ended."""


class PolicyInUseError(RuntimeError):
    """A session on the policy has a queued or started event."""

    def __init__(self, policy_id: str) -> None:
        super().__init__(
            f"policy '{policy_id}' has sessions with queued or active events"
        )
        self.policy_id = policy_id

"""Types for runtime event logging."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class RuntimeEventType(str, Enum):
    """The events a request writes, named after the span each one bounds.

    A GenAI operation's span uses the operation name (``invoke_agent``,
    ``chat``, ``execute_tool``); a span with no GenAI equivalent uses its
    ``mash.*`` name. Each span has one ``.start`` and one ``.end``; an end
    carries ``status`` (see ``EndStatus``). Point-in-time events use their log
    record name.
    """

    INVOKE_AGENT_START = "invoke_agent.start"
    INVOKE_AGENT_END = "invoke_agent.end"
    CONTEXT_LOAD_START = "mash.context_load.start"
    CONTEXT_LOAD_END = "mash.context_load.end"
    CHAT_START = "chat.start"
    CHAT_DELTA = "chat.delta"
    CHAT_END = "chat.end"
    EXECUTE_TOOL_START = "execute_tool.start"
    EXECUTE_TOOL_END = "execute_tool.end"
    STEP_END = "mash.step.end"
    INTERACTION_START = "mash.interaction.start"
    INTERACTION_END = "mash.interaction.end"
    REQUEST_RESUMED = "mash.request.resumed"
    REQUEST_MODE_CHANGED = "mash.request.mode_changed"


class EndStatus(str, Enum):
    """How a span ended: the ``status`` field of every ``.end`` event."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


def end_status(event: "RuntimeEvent") -> Optional[str]:
    """The ``status`` an ``.end`` event carries, or None for other events."""
    if not event.event_type.endswith(".end"):
        return None
    status = (event.payload or {}).get("status")
    return status if status in {s.value for s in EndStatus} else EndStatus.OK.value


def event_attributes(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The OpenTelemetry attributes an event stores under ``attributes``."""
    attributes = (payload or {}).get("attributes")
    return dict(attributes) if isinstance(attributes, dict) else {}


class RequestStatus(str, Enum):
    """A request's lifecycle state, recorded rather than re-derived.

    Events that move a request between states carry one of these on
    ``RuntimeEvent.lifecycle``; the store projects it onto ``runtime_request``.
    Terminality is then a fact about the request, not an inference from event
    type names and their order in the log.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self is not RequestStatus.RUNNING


class RequestMode(str, Enum):
    """Whether the conversation waits on a request.

    ``fg``: a client reads its stream and the user's next message comes after
    it. ``bg``: the user keeps talking and the client reports it when it ends
    or needs input. The mode says nothing about how the request runs.
    """

    FG = "fg"
    BG = "bg"


# ``started_by`` of a request the user started; an agent-started request
# records the starting agent's id instead.
STARTED_BY_USER = "user"


class FeedbackType(str, Enum):
    """Kind of user feedback. Free-form text today; room for future kinds."""

    TEXT = "text"


@dataclass(frozen=True)
class FeedbackRecord:
    """One piece of user feedback captured with its session context."""

    app_id: str
    message: str
    feedback_type: str = FeedbackType.TEXT.value
    feedback_id: int = 0
    host_id: Optional[str] = None
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RuntimeEvent:
    """One durable runtime event in append-only storage."""

    app_id: str
    agent_id: str
    event_type: str
    event_id: int = 0
    request_id: Optional[str] = None
    request_seq: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    trace_id: Optional[str] = None
    session_id: Optional[str] = None
    host_id: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    loop_index: Optional[int] = None
    step_key: Optional[str] = None
    dedupe_key: Optional[str] = None
    # Set only by the events that move a request between lifecycle states;
    # None on everything else. The store projects it onto runtime_request.
    lifecycle: Optional[RequestStatus] = None


@dataclass(frozen=True)
class RequestIdentity:
    """The agent a request was accepted for, and the user it acts for."""

    agent_id: str
    user_id: Optional[str] = None


def accepted_user_id(event: RuntimeEvent) -> Optional[str]:
    """The ``user_id`` an accepted event's request carries, or None."""
    if event.event_type != RuntimeEventType.INVOKE_AGENT_START.value:
        return None
    request_metadata = (event.payload or {}).get("request_metadata") or {}
    user_id = request_metadata.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


@dataclass(frozen=True)
class AcceptedRequest:
    """What an accepted event records about how its request was started."""

    session_id: Optional[str]
    mode: str
    started_by: str
    started_at: float
    parent_request_id: Optional[str]


def accepted_request(event: RuntimeEvent) -> Optional[AcceptedRequest]:
    """The start facts an accepted event carries, or None for other events."""
    if event.event_type != RuntimeEventType.INVOKE_AGENT_START.value:
        return None
    payload = event.payload or {}
    mode = payload.get("mode")
    started_by = payload.get("started_by")
    parent_request_id = payload.get("parent_request_id")
    return AcceptedRequest(
        session_id=event.session_id,
        mode=mode if mode in {m.value for m in RequestMode} else RequestMode.FG.value,
        started_by=(
            started_by
            if isinstance(started_by, str) and started_by
            else STARTED_BY_USER
        ),
        started_at=float(event.created_at),
        parent_request_id=(
            parent_request_id
            if isinstance(parent_request_id, str) and parent_request_id
            else None
        ),
    )

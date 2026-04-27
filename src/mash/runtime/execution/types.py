"""Types for runtime durable event sourcing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class RuntimeEventType(str, Enum):
    REQUEST_ACCEPTED = "runtime.request.accepted"
    REQUEST_WAITING = "runtime.request.waiting"
    TRACE_STARTED = "runtime.trace.started"
    SESSION_RESOLVED = "runtime.session.resolved"
    CONTEXT_LOADED = "runtime.context.loaded"
    LLM_THINK_COMPLETED = "runtime.llm.think.completed"
    TOOL_CALL_COMPLETED = "runtime.tool.call.completed"
    SUBAGENT_CALL_COMPLETED = "runtime.subagent.call.completed"
    TURN_PERSISTED = "runtime.turn.persisted"
    REQUEST_COMPLETED = "runtime.request.completed"
    REQUEST_FAILED = "runtime.request.failed"
    STEP_FAILED = "runtime.step.failed"


@dataclass(frozen=True)
class RuntimeEvent:
    """One durable runtime event in append-only storage."""

    request_id: str
    app_id: str
    agent_id: str
    event_type: str
    seq: int = 0
    trace_id: Optional[str] = None
    session_id: Optional[str] = None
    loop_index: Optional[int] = None
    step_key: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class RuntimeReplayState:
    """In-memory state rebuilt by replaying runtime events."""

    request_id: str
    app_id: str
    agent_id: str
    message: str
    initial_session_id: Optional[str] = None
    turn_metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    session_id: Optional[str] = None
    context_payload: Optional[Dict[str, Any]] = None
    terminal_payload: Optional[Dict[str, Any]] = None
    failure_payload: Optional[Dict[str, Any]] = None
    events: list[RuntimeEvent] = field(default_factory=list)
    loop_actions: dict[int, Dict[str, Any]] = field(default_factory=dict)
    loop_results: dict[int, list[RuntimeEvent]] = field(default_factory=dict)
    step_failures: dict[str, list[RuntimeEvent]] = field(default_factory=dict)
    turn_persisted_payload: Optional[Dict[str, Any]] = None

    @property
    def is_terminal(self) -> bool:
        return self.terminal_payload is not None or self.failure_payload is not None

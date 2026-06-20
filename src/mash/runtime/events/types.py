"""Types for runtime event logging."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class RuntimeEventType(str, Enum):
    REQUEST_ACCEPTED = "runtime.request.accepted"
    TRACE_STARTED = "runtime.trace.started"
    CONTEXT_LOADED = "runtime.context.loaded"
    LLM_THINK_STARTED = "runtime.llm.think.started"
    LLM_THINK_COMPLETED = "runtime.llm.think.completed"
    TOOL_CALL_STARTED = "runtime.tool.call.started"
    TOOL_CALL_COMPLETED = "runtime.tool.call.completed"
    SUBAGENT_CALL_COMPLETED = "runtime.subagent.call.completed"
    STEP_COMPLETED = "runtime.step.completed"
    TURN_PERSISTED = "runtime.turn.persisted"
    INTERACTION_CREATE = "runtime.interaction.create"
    INTERACTION_ACK = "runtime.interaction.ack"
    REQUEST_COMPLETED = "runtime.request.completed"
    REQUEST_FAILED = "runtime.request.failed"
    STEP_FAILED = "runtime.step.failed"


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

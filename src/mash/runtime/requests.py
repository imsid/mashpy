"""Request lifecycle and SSE event helpers."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Optional

from ..logging import AgentTraceEvent, CommandEvent, DebugEvent, LLMEvent
from .errors import classify_error
from .events import RuntimeEvent, RuntimeEventType

if TYPE_CHECKING:
    from .service import AgentRuntime


async def submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
) -> dict[str, Any]:
    return await _submit_request(
        self,
        message=message,
        session_id=session_id,
        request_metadata=None,
    )


async def submit_subagent_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    primary_session_id: str,
    primary_app_id: str,
    subagent_id: str,
    subagent_invoke_opts: dict[str, Any],
) -> dict[str, Any]:
    return await _submit_request(
        self,
        message=message,
        session_id=session_id,
        request_metadata={
            "primary_session_id": primary_session_id,
            "primary_app_id": primary_app_id,
            "subagent_id": subagent_id,
            "subagent_invoke_opts": dict(subagent_invoke_opts),
        },
    )


async def _submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    request_metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    self.require_open()
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    target_session_id = session_id.strip()
    if not target_session_id:
        raise ValueError("session_id is required")

    request_id = str(uuid.uuid4())
    workflow_id = f"{self.app_id}:{request_id}"
    accepted_event = await append_runtime_event(
        self,
        RuntimeEvent(
            request_id=request_id,
            app_id=self.app_id,
            agent_id=self.app_id,
            session_id=target_session_id,
            event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
            dedupe_key="request.accepted",
            payload={
                "workflow_id": workflow_id,
                "message": message,
                "initial_session_id": target_session_id,
                "request_metadata": dict(request_metadata or {}),
            },
        ),
    )
    try:
        await self.engine.start_request(
            request_id=request_id,
            message=message,
            session_id=target_session_id,
            request_metadata=dict(request_metadata or {}),
        )
    except Exception as exc:
        await append_runtime_event(
            self,
            RuntimeEvent(
                request_id=request_id,
                app_id=self.app_id,
                agent_id=self.app_id,
                session_id=target_session_id,
                event_type=RuntimeEventType.REQUEST_FAILED.value,
                dedupe_key="request.failed",
                payload={
                    "request_id": request_id,
                    "agent_id": self.app_id,
                    "status": "error",
                    "session_id": target_session_id,
                    **classify_error(exc),
                },
            ),
        )
        raise
    return to_public_event(accepted_event)["data"]


async def stream_response_events(
    self: "AgentRuntime",
    request_id: str,
    *,
    cursor: int = 0,
    wait_timeout: float = 15.0,
) -> tuple[list[dict[str, Any]], int, bool]:
    self.require_open()
    if not await self.runtime_store.has_request(request_id):
        raise KeyError(request_id)

    stored_events = await self.runtime_store.list_request_events(
        request_id,
        after_seq=max(0, int(cursor)),
    )
    public_events = [to_public_event(event) for event in stored_events]
    next_cursor = int(cursor)
    if stored_events:
        next_cursor = int(stored_events[-1].request_seq or 0)
    done = await self.runtime_store.is_request_terminal(request_id)
    if public_events or done or wait_timeout <= 0:
        return public_events, next_cursor, done

    await asyncio.sleep(max(0.0, wait_timeout))
    return [], next_cursor, False


async def append_runtime_event(
    self: "AgentRuntime",
    event: RuntimeEvent,
) -> RuntimeEvent:
    return await self.runtime_store.append_event(event)


def to_public_event(event: RuntimeEvent) -> dict[str, Any]:
    if event.event_type == RuntimeEventType.REQUEST_ACCEPTED.value:
        return {
            "event": "request.accepted",
            "data": {
                "request_id": event.request_id,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "status": "accepted",
            },
        }
    if event.event_type == RuntimeEventType.TRACE_STARTED.value:
        return {
            "event": "request.started",
            "data": {
                "request_id": event.request_id,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "status": "started",
            },
        }
    if event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
        return {"event": "request.completed", "data": dict(event.payload or {})}
    if event.event_type == RuntimeEventType.REQUEST_FAILED.value:
        return {"event": "request.error", "data": dict(event.payload or {})}
    return {
        "event": "agent.trace",
        "data": {
            "event_type": event.event_type,
            "trace_id": event.trace_id,
            "loop_index": event.loop_index,
            "step_key": event.step_key,
            "payload": dict(event.payload or {}),
        },
    }


def to_trace_payload(event: Any) -> Optional[dict[str, Any]]:
    if isinstance(event, AgentTraceEvent):
        return {
            "event_type": event.event_type,
            "trace_id": event.trace_id,
            "step_id": event.step_id,
            "duration_ms": event.duration_ms,
            "action_type": event.action_type,
            "tool_calls": event.tool_calls,
            "token_usage": event.token_usage,
            "payload": dict(event.payload or {}),
        }

    if isinstance(event, LLMEvent):
        return {
            "event_type": event.event_type,
            "trace_id": event.trace_id,
            "provider": event.provider,
            "model": event.model,
            "duration_ms": event.duration_ms,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
            "finish_reason": event.finish_reason,
            "error": event.error,
            "tools": event.tools,
            "betas": event.betas,
        }

    if isinstance(event, CommandEvent):
        return {
            "event_type": event.event_type,
            "trace_id": event.trace_id,
            "duration_ms": event.duration_ms,
            "payload": dict(event.payload or {}),
        }

    if isinstance(event, DebugEvent):
        return {
            "event_type": event.event_type,
            "payload": dict(event.payload or {}),
        }

    return None

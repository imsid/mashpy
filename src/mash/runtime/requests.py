"""Request lifecycle and SSE event helpers."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from ..logging import AgentTraceEvent, CommandEvent, DebugEvent, LLMEvent
from ..logging.trace_context import (
    bound_host_id,
    clear_subagent_request_id,
    get_host_id,
    get_subagent_request_id,
    get_workflow_id,
    get_workflow_run_id,
)
from .errors import RequestStaleError, classify_error
from .events import RuntimeEvent, RuntimeEventType
from .structured_output import serialize_structured_output

if TYPE_CHECKING:
    from .service import AgentRuntime


async def submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    structured_output: Any = None,
    host_snapshot: dict[str, Any] | None = None,
    context: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_structured_output = serialize_structured_output(structured_output)
    request_metadata: dict[str, Any] = {}
    if normalized_structured_output is not None:
        request_metadata["structured_output_request"] = normalized_structured_output
    if host_snapshot is not None:
        request_metadata["host"] = dict(host_snapshot)
    if context is not None and str(context).strip():
        request_metadata["context"] = str(context)
    if metadata:
        request_metadata["metadata"] = dict(metadata)
    return await _submit_request(
        self,
        message=message,
        session_id=session_id,
        request_metadata=request_metadata or None,
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
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_metadata: dict[str, Any] = {
        "primary_session_id": primary_session_id,
        "primary_app_id": primary_app_id,
        "subagent_id": subagent_id,
        "subagent_invoke_opts": dict(subagent_invoke_opts),
    }
    if metadata:
        request_metadata["metadata"] = dict(metadata)
    return await _submit_request(
        self,
        message=message,
        session_id=session_id,
        request_metadata=request_metadata,
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

    with bound_host_id(host_id_from_request_metadata(request_metadata)):
        return await _submit_request_inner(
            self,
            message=message,
            session_id=target_session_id,
            request_metadata=request_metadata,
        )


def host_id_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[str]:
    host = dict(request_metadata or {}).get("host")
    if not isinstance(host, dict):
        return None
    host_id = str(host.get("host_id") or "").strip()
    return host_id or None


def find_pending_interaction(
    events: list[RuntimeEvent],
) -> Optional[str]:
    """Return the interaction id of the request's last un-acked interaction.

    An interaction is pending when its ``interaction.create`` has no matching
    ``interaction.ack``. The cancel path uses this to clear a parked prompt and
    unblock the workflow's ``recv`` with the cancel sentinel.
    """
    pending: dict[str, None] = {}
    for event in events:
        payload = event.payload or {}
        interaction_id = payload.get("interaction_id")
        if not interaction_id:
            continue
        if event.event_type == RuntimeEventType.INTERACTION_CREATE.value:
            pending[str(interaction_id)] = None
        elif event.event_type == RuntimeEventType.INTERACTION_ACK.value:
            pending.pop(str(interaction_id), None)
    if not pending:
        return None
    # dicts preserve insertion order; the last still-open create wins.
    return next(reversed(pending))


def caller_metadata_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    metadata = dict(request_metadata or {}).get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return None
    return metadata


async def _submit_request_inner(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    request_metadata: Optional[dict[str, Any]],
) -> dict[str, Any]:
    target_session_id = session_id
    # A subagent invocation binds a deterministic request id so a DBOS replay
    # reissues the child under the same workflow id and reattaches to the one
    # existing child instead of starting a second. Consume it before the child
    # workflow task is spawned so the child never inherits the parent's key.
    deterministic_request_id = get_subagent_request_id()
    if deterministic_request_id:
        clear_subagent_request_id()
        request_id = deterministic_request_id
    else:
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

    waiter = self.runtime_store.register_request_waiter(request_id)
    try:
        try:
            await asyncio.wait_for(waiter.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            pass
        stored_events = await self.runtime_store.list_request_events(
            request_id,
            after_seq=max(0, int(cursor)),
        )
        public_events = [to_public_event(event) for event in stored_events]
        if stored_events:
            next_cursor = int(stored_events[-1].request_seq or 0)
        done = await self.runtime_store.is_request_terminal(request_id)
        return public_events, next_cursor, done
    finally:
        self.runtime_store.unregister_request_waiter(request_id, waiter)


def _session_has_later_turn(
    turns: list[dict[str, Any]],
    *,
    accepted_at: float,
    request_trace_id: str | None,
) -> bool:
    """True if a replayable turn was persisted after the request was accepted.

    Excludes the request's own turn (matched by trace id) so re-resuming a
    completed request is not treated as stale.
    """
    for turn in turns:
        if not bool(turn.get("replayable", True)):
            continue
        if request_trace_id and str(turn.get("trace_id")) == str(request_trace_id):
            continue
        if float(turn.get("created_at") or 0.0) > accepted_at:
            return True
    return False


async def resume_request(
    self: "AgentRuntime",
    request_id: str,
) -> dict[str, Any]:
    self.require_open()
    events = await self.runtime_store.list_request_events(request_id)
    if not events:
        raise KeyError(request_id)
    accepted = next(
        (
            e
            for e in events
            if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value
        ),
        None,
    )
    accepted_at = float(accepted.created_at) if accepted is not None else 0.0
    session_id = next((e.session_id for e in events if e.session_id), None)
    request_trace_id = next((e.trace_id for e in events if e.trace_id), None)

    # A request that already completed has nothing to replay, so the engine's
    # idempotent response is the honest answer even once the session has moved
    # on. Checking it first keeps a completed request from reporting stale.
    last_lifecycle = next(
        (
            e.event_type
            for e in reversed(events)
            if e.event_type
            in (
                RuntimeEventType.REQUEST_COMPLETED.value,
                RuntimeEventType.REQUEST_RESUMED.value,
            )
        ),
        None,
    )
    already_completed = last_lifecycle == RuntimeEventType.REQUEST_COMPLETED.value

    # Stale-session guard: resume replays the request's original context
    # snapshot, so it is unsafe once the session has a newer replayable turn.
    if session_id and not already_completed:
        turns = await self.store.get_turns(session_id, self.app_id)
        if _session_has_later_turn(
            turns,
            accepted_at=accepted_at,
            request_trace_id=request_trace_id,
        ):
            raise RequestStaleError(
                f"request '{request_id}' cannot be resumed: its session has "
                f"newer turns"
            )

    result = await self.engine.resume_request(request_id=request_id)

    if str(result.get("status")) == "resumed":
        # Non-terminal lifecycle event. Because terminality reads the last
        # event, appending it after a request.failed / request.cancelled flips
        # the request back to non-terminal so closed streams re-open. A distinct
        # dedupe_key per resume (by prior resumed count) lets a request be
        # resumed more than once.
        prior_resumes = sum(
            1
            for e in events
            if e.event_type == RuntimeEventType.REQUEST_RESUMED.value
        )
        await append_runtime_event(
            self,
            RuntimeEvent(
                request_id=request_id,
                app_id=self.app_id,
                agent_id=self.app_id,
                trace_id=request_trace_id,
                session_id=session_id,
                event_type=RuntimeEventType.REQUEST_RESUMED.value,
                dedupe_key=f"request.resumed.{prior_resumes}",
                payload={
                    "request_id": request_id,
                    "agent_id": self.app_id,
                    "session_id": session_id,
                    "status": "resumed",
                    "previous_status": result.get("previous_status"),
                },
            ),
        )
    return result


async def rerun_request(
    self: "AgentRuntime",
    request_id: str,
) -> dict[str, Any]:
    """Start a previous request over as a brand-new request.

    Rebuilds from the original ``request.accepted`` payload (message + full
    request metadata, including the host snapshot and structured-output
    request), stamps ``rerun_of`` provenance, and submits through the normal
    path — new request id, new trace, and a fresh ``context.load`` against the
    session's current history. Works for any previous request regardless of its
    terminal state.
    """
    self.require_open()
    events = await self.runtime_store.list_request_events(request_id)
    accepted = next(
        (
            e
            for e in events
            if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value
        ),
        None,
    )
    if accepted is None:
        raise KeyError(request_id)
    payload = accepted.payload or {}
    message = str(payload.get("message") or "")
    session_id = str(payload.get("initial_session_id") or accepted.session_id or "")
    request_metadata = dict(payload.get("request_metadata") or {})
    # Provenance for code and the UI; a top-level key, never shown to the model
    # (which only sees host/context, not sibling metadata keys).
    request_metadata["rerun_of"] = request_id
    return await _submit_request(
        self,
        message=message,
        session_id=session_id,
        request_metadata=request_metadata or None,
    )


async def append_runtime_event(
    self: "AgentRuntime",
    event: RuntimeEvent,
) -> RuntimeEvent:
    if event.host_id is None:
        bound = get_host_id()
        if bound is not None:
            event = replace(event, host_id=bound)
    if event.workflow_id is None:
        bound_workflow_id = get_workflow_id()
        if bound_workflow_id is not None:
            event = replace(event, workflow_id=bound_workflow_id)
    if event.workflow_run_id is None:
        bound_run_id = get_workflow_run_id()
        if bound_run_id is not None:
            event = replace(event, workflow_run_id=bound_run_id)
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
    if event.event_type == RuntimeEventType.INTERACTION_CREATE.value:
        return {
            "event": "request.interaction.create",
            "data": {
                "request_id": event.request_id,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "interaction_id": event.payload.get("interaction_id"),
                "type": event.payload.get("type"),
                "prompt": event.payload.get("prompt"),
                "schema": event.payload.get("schema"),
                "timeout_seconds": event.payload.get("timeout_seconds"),
            },
        }
    if event.event_type == RuntimeEventType.INTERACTION_ACK.value:
        return {
            "event": "request.interaction.ack",
            "data": {
                "request_id": event.request_id,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "interaction_id": event.payload.get("interaction_id"),
                "response": event.payload.get("response"),
            },
        }
    if event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
        return {"event": "request.completed", "data": dict(event.payload or {})}
    if event.event_type == RuntimeEventType.REQUEST_FAILED.value:
        return {"event": "request.error", "data": dict(event.payload or {})}
    if event.event_type == RuntimeEventType.REQUEST_CANCELLED.value:
        return {"event": "request.cancelled", "data": dict(event.payload or {})}
    if event.event_type == RuntimeEventType.REQUEST_RESUMED.value:
        return {"event": "request.resumed", "data": dict(event.payload or {})}
    return {
        "event": "agent.trace",
        "data": {
            "event_type": event.event_type,
            "trace_id": event.trace_id,
            "session_id": event.session_id,
            "loop_index": event.loop_index,
            "step_key": event.step_key,
            "created_at": float(event.created_at),
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

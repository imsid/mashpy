"""Request lifecycle and SSE event helpers."""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from ..logging.trace_context import (
    bound_host_id,
    bound_workflow_ids,
    get_host_id,
    get_workflow_id,
    get_workflow_run_id,
)
from .errors import RequestStaleError, RequestTerminalError, classify_error
from ..otel import semconv
from .events import RequestMode, RequestStatus, RuntimeEvent, RuntimeEventType
from .events.types import STARTED_BY_USER, EndStatus, end_status
from .structured_output import serialize_structured_output

if TYPE_CHECKING:
    from .service import AgentRuntime


# Caller-supplied request ids end up in URLs and in subagent and workflow
# request ids, so they are kept to a conservative charset.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,255}$")


class RequestIdConflictError(ValueError):
    """A caller-supplied request id already belongs to another agent."""


def validate_request_id(request_id: Any) -> str:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
        raise ValueError(
            "request_id must be 1-255 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return request_id


def validate_user_id(user_id: Any) -> str:
    if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
        raise ValueError(
            "user_id must be 1-255 characters of letters, digits, '.', '_', ':', '@' or '-'"
        )
    return user_id


def validate_mode(mode: Any) -> str:
    if mode not in {m.value for m in RequestMode}:
        raise ValueError("mode must be 'fg' or 'bg'")
    return str(mode)


def _require_str(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"parent.{key} is required")
    return value.strip()


def parent_request_metadata(parent: Any, *, agent_id: str) -> dict[str, Any]:
    """Map an H2A ``parent`` object onto the request metadata the engine reads.

    The engine keeps subagent and workflow turns out of replayed conversation
    history by these keys, and binds workflow ids onto the request's events.
    """
    if not isinstance(parent, dict):
        raise ValueError("parent must be an object")
    kind = parent.get("kind")
    if kind == "subagent":
        invoke_opts = parent.get("invoke_opts")
        if invoke_opts is None:
            invoke_opts = {}
        if not isinstance(invoke_opts, dict):
            raise ValueError("parent.invoke_opts must be an object")
        normalized = {
            "kind": "subagent",
            "request_id": _require_str(parent, "request_id"),
            "agent_id": _require_str(parent, "agent_id"),
            "session_id": _require_str(parent, "session_id"),
            "invoke_opts": dict(invoke_opts),
            "started_by": _require_str(parent, "started_by"),
            "parent_request_id": _require_str(parent, "parent_request_id"),
        }
        # The child's row records who started it and from which request;
        # both name the parent, so they must agree with it.
        if normalized["started_by"] != normalized["agent_id"]:
            raise ValueError("parent.started_by must be parent.agent_id")
        if normalized["parent_request_id"] != normalized["request_id"]:
            raise ValueError("parent.parent_request_id must be parent.request_id")
        return {
            "parent": normalized,
            "primary_session_id": normalized["session_id"],
            "primary_app_id": normalized["agent_id"],
            "subagent_id": agent_id,
            "subagent_invoke_opts": dict(invoke_opts),
        }
    if kind == "workflow":
        normalized = {
            "kind": "workflow",
            "workflow_id": _require_str(parent, "workflow_id"),
            "run_id": _require_str(parent, "run_id"),
            "task_id": _require_str(parent, "task_id"),
        }
        return {
            "parent": normalized,
            "workflow_id": normalized["workflow_id"],
            "workflow_run_id": normalized["run_id"],
            "task_id": normalized["task_id"],
        }
    raise ValueError("parent.kind must be 'subagent' or 'workflow'")


async def get_accepted_request(
    self: "AgentRuntime",
    request_id: str,
) -> dict[str, Any] | None:
    """Return the accepted payload of an existing request, or None.

    Raises ``RequestIdConflictError`` when the id belongs to another agent.
    """
    events = await self.runtime_store.list_request_events(request_id)
    accepted = next(
        (
            event
            for event in events
            if event.event_type == RuntimeEventType.INVOKE_AGENT_START.value
        ),
        None,
    )
    if accepted is None:
        return None
    if accepted.agent_id != self.app_id:
        raise RequestIdConflictError(
            f"request_id '{request_id}' belongs to another agent"
        )
    return to_public_event(accepted)["data"]


async def submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    user_id: str,
    structured_output: Any = None,
    host_snapshot: dict[str, Any] | None = None,
    context: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    parent: dict[str, Any] | None = None,
    mode: str = RequestMode.FG.value,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    """Submit one request. A known ``request_id`` returns the existing request.

    ``traceparent``/``tracestate`` are a caller's W3C trace context; the
    request's spans export under that trace.
    """
    if request_id is not None:
        validate_request_id(request_id)
    validate_user_id(user_id)
    validate_mode(mode)
    normalized_structured_output = serialize_structured_output(structured_output)
    # ``user_id`` is caller context like ``metadata``: stored with the
    # request, never shown to the model.
    request_metadata: dict[str, Any] = {"user_id": user_id}
    if parent is not None:
        request_metadata.update(parent_request_metadata(parent, agent_id=self.app_id))
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
        request_metadata=request_metadata,
        request_id=request_id,
        mode=mode,
        traceparent=traceparent,
        tracestate=tracestate,
    )


async def _submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    request_metadata: Optional[dict[str, Any]],
    request_id: str | None = None,
    mode: str = RequestMode.FG.value,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    self.require_open()
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    target_session_id = session_id.strip()
    if not target_session_id:
        raise ValueError("session_id is required")

    if request_id is not None:
        existing = await get_accepted_request(self, request_id)
        if existing is not None:
            return existing

    # Only a workflow-parented request binds workflow ids here; binding None
    # would hide ids an enclosing workflow step already bound.
    metadata_view = request_metadata or {}
    workflow_scope = nullcontext()
    if metadata_view.get("workflow_id"):
        workflow_scope = bound_workflow_ids(
            metadata_view["workflow_id"], metadata_view.get("workflow_run_id")
        )
    with bound_host_id(host_id_from_request_metadata(request_metadata)), workflow_scope:
        return await _submit_request_inner(
            self,
            message=message,
            session_id=target_session_id,
            request_metadata=request_metadata,
            request_id=request_id,
            mode=mode,
            traceparent=traceparent,
            tracestate=tracestate,
        )


def host_id_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[str]:
    host = dict(request_metadata or {}).get("host")
    if not isinstance(host, dict):
        return None
    host_id = str(host.get("host_id") or "").strip()
    return host_id or None


def request_attempt(events: list[RuntimeEvent]) -> int:
    """Which attempt the request is on: 0 before any resume, +1 per resume.

    Terminal dedupe keys are scoped by this. A fixed key would make the second
    attempt's terminal event dedupe onto the first attempt's row and append
    nothing, leaving ``mash.request.resumed`` as the last lifecycle event and
    the request non-terminal forever.
    """
    return sum(
        1
        for event in events
        if event.event_type == RuntimeEventType.REQUEST_RESUMED.value
    )


async def fetch_request_attempt(self: "AgentRuntime", request_id: str) -> int:
    """``request_attempt`` over the request's stored events."""
    return request_attempt(await self.runtime_store.list_request_events(request_id))


def find_pending_interaction(
    events: list[RuntimeEvent],
) -> Optional[str]:
    """Return the interaction id of the request's last un-acked interaction.

    An interaction is pending when its ``mash.interaction.start`` has no
    matching ``mash.interaction.end``. The cancel path uses this to clear a parked prompt and
    unblock the workflow's ``recv`` with the cancel sentinel.
    """
    pending: dict[str, None] = {}
    for event in events:
        payload = event.payload or {}
        interaction_id = payload.get("interaction_id")
        if not interaction_id:
            continue
        if event.event_type == RuntimeEventType.INTERACTION_START.value:
            pending[str(interaction_id)] = None
        elif event.event_type == RuntimeEventType.INTERACTION_END.value:
            pending.pop(str(interaction_id), None)
    if not pending:
        return None
    # dicts preserve insertion order; the last still-open create wins.
    return next(reversed(pending))


def user_id_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[str]:
    user_id = dict(request_metadata or {}).get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


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
    request_id: str | None = None,
    mode: str = RequestMode.FG.value,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    target_session_id = session_id
    request_id = request_id or str(uuid.uuid4())
    started_by = STARTED_BY_USER
    parent_request_id = None
    parent = (request_metadata or {}).get("parent")
    if isinstance(parent, dict) and parent.get("kind") == "subagent":
        started_by = str(parent.get("started_by") or parent.get("agent_id"))
        parent_request_id = parent.get("parent_request_id") or parent.get("request_id")
    host_id = host_id_from_request_metadata(request_metadata)
    attributes: dict[str, Any] = {
        semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
        semconv.GEN_AI_AGENT_ID: self.app_id,
        semconv.GEN_AI_AGENT_NAME: self.app_id,
        semconv.GEN_AI_CONVERSATION_ID: target_session_id,
        semconv.MASH_REQUEST_ID: request_id,
        semconv.MASH_REQUEST_MODE: mode,
    }
    if host_id:
        attributes[semconv.MASH_HOST_ID] = host_id
    payload: dict[str, Any] = {
        "message": message,
        "initial_session_id": target_session_id,
        "request_metadata": dict(request_metadata or {}),
        "mode": mode,
        "started_by": started_by,
        "parent_request_id": parent_request_id,
        "attributes": attributes,
    }
    if traceparent:
        payload["traceparent"] = traceparent
        if tracestate:
            payload["tracestate"] = tracestate
    # The trace id is minted here, on the request's first event, so every
    # event of the request (this one included) belongs to its trace. A replay
    # of the accept dedupes onto this row and keeps the id.
    accepted_event = await append_runtime_event(
        self,
        RuntimeEvent(
            request_id=request_id,
            app_id=self.app_id,
            agent_id=self.app_id,
            trace_id=str(uuid.uuid4()),
            session_id=target_session_id,
            event_type=RuntimeEventType.INVOKE_AGENT_START.value,
            dedupe_key="request.accepted",
            lifecycle=RequestStatus.RUNNING,
            payload=payload,
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
        error = classify_error(exc)
        await append_runtime_event(
            self,
            RuntimeEvent(
                request_id=request_id,
                app_id=self.app_id,
                agent_id=self.app_id,
                trace_id=accepted_event.trace_id,
                session_id=target_session_id,
                event_type=RuntimeEventType.INVOKE_AGENT_END.value,
                # Submit-time failure: the request has never been resumed.
                dedupe_key="request.failed.0",
                lifecycle=RequestStatus.FAILED,
                payload={
                    "request_id": request_id,
                    "agent_id": self.app_id,
                    "status": EndStatus.ERROR.value,
                    "session_id": target_session_id,
                    **error,
                    "attributes": {
                        semconv.ERROR_TYPE: error.get("error_type") or "error",
                    },
                },
            ),
        )
        raise
    return to_public_event(accepted_event)["data"]


async def set_request_mode(
    self: "AgentRuntime",
    request_id: str,
    mode: str,
) -> dict[str, Any]:
    """Switch a running request between ``fg`` and ``bg``.

    Raises ``KeyError`` for an unknown request and ``RequestTerminalError``
    once the request has finished.
    """
    self.require_open()
    validate_mode(mode)
    events = await self.runtime_store.list_request_events(request_id)
    accepted = next(
        (
            e
            for e in events
            if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value
        ),
        None,
    )
    if accepted is None or accepted.agent_id != self.app_id:
        raise KeyError(request_id)
    status = await self.runtime_store.get_request_lifecycle(request_id)
    if status is not None and status.terminal:
        raise RequestTerminalError(
            f"request '{request_id}' is {status.value}; its mode cannot change"
        )
    # No dedupe key: a request can switch any number of times, and seq
    # orders the switches.
    await append_runtime_event(
        self,
        RuntimeEvent(
            request_id=request_id,
            app_id=self.app_id,
            agent_id=self.app_id,
            trace_id=next((e.trace_id for e in events if e.trace_id), None),
            session_id=accepted.session_id,
            event_type=RuntimeEventType.REQUEST_MODE_CHANGED.value,
            payload={"mode": mode, "attributes": {semconv.MASH_REQUEST_MODE: mode}},
        ),
    )
    return {"request_id": request_id, "agent_id": self.app_id, "mode": mode}


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

    # One store call for both facts: `done` is scoped to the events it returns,
    # so a caller that stops on `done` has necessarily been given the terminal
    # event. Reading them separately let an append between the two land a
    # `done=True` alongside events truncated before the terminal row, and the
    # stream closed without ever emitting it.
    stored_events, done = await self.runtime_store.read_request_stream(
        request_id,
        after_seq=max(0, int(cursor)),
    )
    public_events = [to_public_event(event) for event in stored_events]
    next_cursor = int(cursor)
    if stored_events:
        next_cursor = int(stored_events[-1].request_seq or 0)
    if public_events or done or wait_timeout <= 0:
        return public_events, next_cursor, done

    waiter = self.runtime_store.register_request_waiter(request_id)
    try:
        try:
            await asyncio.wait_for(waiter.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            pass
        stored_events, done = await self.runtime_store.read_request_stream(
            request_id,
            after_seq=max(0, int(cursor)),
        )
        public_events = [to_public_event(event) for event in stored_events]
        if stored_events:
            next_cursor = int(stored_events[-1].request_seq or 0)
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
            if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value
        ),
        None,
    )
    accepted_at = float(accepted.created_at) if accepted is not None else 0.0
    session_id = next((e.session_id for e in events if e.session_id), None)
    request_trace_id = next((e.trace_id for e in events if e.trace_id), None)

    # A completed request has nothing to replay, so it answers completed even
    # once the session has moved on, rather than reporting stale.
    status = await self.runtime_store.get_request_lifecycle(request_id)

    # Stale-session guard: resume replays the request's original context
    # snapshot, so it is unsafe once the session has a newer replayable turn.
    if session_id and status is not RequestStatus.COMPLETED:
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

    if status is RequestStatus.COMPLETED:
        return {
            "request_id": request_id,
            "status": "completed",
            "message": "request already completed successfully",
        }
    if status is RequestStatus.RUNNING:
        return {
            "request_id": request_id,
            "status": "running",
            "message": "request is already running",
        }

    # Recorded before the loop restarts, so the new attempt's terminal event
    # gets a dedupe key of its own (see request_attempt). Because terminality
    # reads the last lifecycle event, this also re-opens closed streams.
    prior_resumes = request_attempt(events)
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
            lifecycle=RequestStatus.RUNNING,
            payload={
                "request_id": request_id,
                "agent_id": self.app_id,
                "session_id": session_id,
                "status": "resumed",
                "previous_status": status.value if status else None,
                "attributes": {semconv.MASH_REQUEST_ID: request_id},
            },
        ),
    )
    await self.engine.continue_request(request_id)
    return {
        "request_id": request_id,
        "status": "resumed",
        "previous_status": status.value if status else None,
        "message": "request has been resumed from its last step",
    }


async def rerun_request(
    self: "AgentRuntime",
    request_id: str,
) -> dict[str, Any]:
    """Start a previous request over as a brand-new request.

    Rebuilds from the original ``invoke_agent.start`` payload (message + full
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
            if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value
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


_END_FRAMES = {
    EndStatus.OK.value: "request.completed",
    EndStatus.ERROR.value: "request.error",
    EndStatus.CANCELLED.value: "request.cancelled",
}


def to_public_event(event: RuntimeEvent) -> dict[str, Any]:
    """The SSE frame for one stored event.

    Lifecycle events get ``request.*`` frames; a request's end is one
    ``invoke_agent.end`` whose ``status`` picks the frame. Every other event
    goes out as an ``agent.trace`` frame carrying its stored name and payload.
    """
    if event.event_type == RuntimeEventType.INVOKE_AGENT_START.value:
        data = {
            "request_id": event.request_id,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "status": "accepted",
        }
        user_id = user_id_from_request_metadata(
            (event.payload or {}).get("request_metadata")
        )
        if user_id is not None:
            data["user_id"] = user_id
        return {"event": "request.accepted", "data": data}
    if event.event_type == RuntimeEventType.CONTEXT_LOAD_START.value:
        return {
            "event": "request.started",
            "data": {
                "request_id": event.request_id,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "status": "started",
            },
        }
    if event.event_type == RuntimeEventType.INTERACTION_START.value:
        data = {
            "request_id": event.request_id,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "interaction_id": event.payload.get("interaction_id"),
            "type": event.payload.get("type"),
            "prompt": event.payload.get("prompt"),
            "schema": event.payload.get("schema"),
            "timeout_seconds": event.payload.get("timeout_seconds"),
        }
        # A browser handoff's live-view link and when it lapses.
        for field_name in ("url", "expires_at"):
            if field_name in event.payload:
                data[field_name] = event.payload[field_name]
        return {"event": "request.interaction.create", "data": data}
    if event.event_type == RuntimeEventType.INTERACTION_END.value:
        data = {
            "request_id": event.request_id,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "interaction_id": event.payload.get("interaction_id"),
            "response": event.payload.get("response"),
        }
        # Present only when set, so callers can tell a timeout default or a
        # cancel from a real response.
        for flag in ("timed_out", "cancelled"):
            if event.payload.get(flag):
                data[flag] = True
        return {"event": "request.interaction.ack", "data": data}
    if event.event_type == RuntimeEventType.INVOKE_AGENT_END.value:
        frame = _END_FRAMES[end_status(event) or EndStatus.OK.value]
        return {"event": frame, "data": dict(event.payload or {})}
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

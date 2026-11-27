"""Request lifecycle and SSE event helpers."""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError

from ..logging.trace_context import (
    bound_host_id,
    bound_workflow_ids,
    get_host_id,
    get_workflow_id,
    get_workflow_run_id,
)
from .errors import (
    SessionPolicyMismatchError,
    classify_error,
)
from ..otel import semconv
from .events import RequestStatus, RuntimeEvent, RuntimeEventType
from .events.types import STARTED_BY_USER, EndStatus, end_status
from .structured_output import serialize_structured_output
from ..policies import Policy, validate_policy_id

if TYPE_CHECKING:
    from .service import AgentRuntime


# Caller-supplied request ids end up in URLs and in subagent and workflow
# request ids, so they are kept to a conservative charset.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_APP_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,255}$")


class RequestIdConflictError(ValueError):
    """A caller-supplied request id already belongs to another agent."""


def validate_request_id(request_id: Any) -> str:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
        raise ValueError(
            "request_id must be 1-255 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return request_id


def validate_app_id(app_id: Any) -> str:
    if not isinstance(app_id, str) or not _APP_ID_RE.match(app_id):
        raise ValueError(
            "app_id must be 1-255 characters of letters, digits, '.', '_', ':', '@' or '-'"
        )
    return app_id


def validate_policy_snapshot(policy: Any) -> dict[str, Any]:
    """The policy document a request runs under, checked and normalized."""
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")
    try:
        return Policy.model_validate(policy).model_dump()
    except ValidationError as exc:
        raise ValueError(f"invalid policy: {exc}") from exc


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
        event_session_id = parent.get("event_session_id")
        if event_session_id is not None:
            if not isinstance(event_session_id, str) or not event_session_id.strip():
                raise ValueError("parent.event_session_id must be a non-empty string")
            normalized["event_session_id"] = event_session_id.strip()
        return {
            "parent": normalized,
            "event_session_id": normalized.get("event_session_id"),
            "primary_session_id": normalized["session_id"],
            "primary_agent_id": normalized["agent_id"],
            "subagent_id": agent_id,
            "subagent_invoke_opts": dict(invoke_opts),
        }
    if kind == "conversation":
        # A request the companion routed: it runs in the companion's session
        # and reads that session's board.
        normalized = {
            "kind": "conversation",
            "request_id": _require_str(parent, "request_id"),
            "agent_id": _require_str(parent, "agent_id"),
            "session_id": _require_str(parent, "session_id"),
            "started_by": _require_str(parent, "started_by"),
            "parent_request_id": _require_str(parent, "parent_request_id"),
        }
        if normalized["started_by"] != normalized["agent_id"]:
            raise ValueError("parent.started_by must be parent.agent_id")
        if normalized["parent_request_id"] != normalized["request_id"]:
            raise ValueError("parent.parent_request_id must be parent.request_id")
        event_session_id = parent.get("event_session_id")
        if event_session_id is not None:
            if not isinstance(event_session_id, str) or not event_session_id.strip():
                raise ValueError("parent.event_session_id must be a non-empty string")
            normalized["event_session_id"] = event_session_id.strip()
        return {
            "parent": normalized,
            "event_session_id": normalized.get("event_session_id"),
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
    raise ValueError("parent.kind must be 'subagent', 'conversation' or 'workflow'")


def is_conversation_request(request_metadata: dict[str, Any] | None) -> bool:
    """Whether the request is one the companion routed: it reads its session's board."""
    parent = (request_metadata or {}).get("parent")
    return isinstance(parent, dict) and parent.get("kind") == "conversation"


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
    if accepted.agent_id != self.agent_id:
        raise RequestIdConflictError(
            f"request_id '{request_id}' belongs to another agent"
        )
    return to_public_event(accepted)["data"]


async def submit_request(
    self: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    app_id: str,
    policy_id: str,
    policy: dict[str, Any],
    structured_output: Any = None,
    host_snapshot: dict[str, Any] | None = None,
    context: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
    parent: dict[str, Any] | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    """Submit one request. A known ``request_id`` returns the existing request.

    ``traceparent``/``tracestate`` are a caller's W3C trace context; the
    request's spans export under that trace.
    """
    if request_id is not None:
        validate_request_id(request_id)
    validate_app_id(app_id)
    validate_policy_id(policy_id)
    snapshot = validate_policy_snapshot(policy)
    normalized_structured_output = serialize_structured_output(structured_output)
    # The app, the policy and its snapshot are caller context like
    # ``metadata``: stored with the request, never shown to the model.
    request_metadata: dict[str, Any] = {
        "app_id": app_id,
        "policy_id": policy_id,
        "policy": snapshot,
    }
    if parent is not None:
        request_metadata.update(parent_request_metadata(parent, agent_id=self.agent_id))
    else:
        # Only an event's request has no parent: its session is the event's.
        request_metadata["event_session_id"] = session_id
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

    # A session runs under the policy of its first request: its turns replay
    # into every request on it, so a request under another policy is refused.
    policy_id = policy_id_from_request_metadata(request_metadata)
    if policy_id is not None:
        other = await self.runtime_store.get_other_session_policy(target_session_id, policy_id)
        if other is not None:
            raise SessionPolicyMismatchError(
                f"session '{target_session_id}' runs under another policy"
            )

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





def pending_interactions(events: list[RuntimeEvent]) -> list[str]:
    """The ids of the request's open interactions, in the order they opened.

    An interaction is open while its ``mash.interaction.start`` has no
    matching ``mash.interaction.end``. A request waits on one of its own at a
    time, and on one more per child interaction it relays.
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
    return list(pending)


def _metadata_text(request_metadata: Optional[dict[str, Any]], key: str) -> Optional[str]:
    value = dict(request_metadata or {}).get(key)
    return value if isinstance(value, str) and value else None


def app_id_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[str]:
    return _metadata_text(request_metadata, "app_id")


def policy_id_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[str]:
    return _metadata_text(request_metadata, "policy_id")


def policy_from_request_metadata(
    request_metadata: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """The policy snapshot a request runs under, or None."""
    policy = dict(request_metadata or {}).get("policy")
    return dict(policy) if isinstance(policy, dict) else None


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
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    target_session_id = session_id
    request_id = request_id or str(uuid.uuid4())
    started_by = STARTED_BY_USER
    parent_request_id = None
    parent = (request_metadata or {}).get("parent")
    if isinstance(parent, dict) and parent.get("kind") in ("subagent", "conversation"):
        started_by = str(parent.get("started_by") or parent.get("agent_id"))
        parent_request_id = parent.get("parent_request_id") or parent.get("request_id")
    host_id = host_id_from_request_metadata(request_metadata)
    attributes: dict[str, Any] = {
        semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
        semconv.GEN_AI_AGENT_ID: self.agent_id,
        semconv.GEN_AI_AGENT_NAME: self.agent_id,
        semconv.GEN_AI_CONVERSATION_ID: target_session_id,
        semconv.MASH_REQUEST_ID: request_id,
    }
    if host_id:
        attributes[semconv.MASH_HOST_ID] = host_id
    payload: dict[str, Any] = {
        "message": message,
        "initial_session_id": target_session_id,
        "request_metadata": dict(request_metadata or {}),
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
            agent_id=self.agent_id,
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
                agent_id=self.agent_id,
                trace_id=accepted_event.trace_id,
                session_id=target_session_id,
                event_type=RuntimeEventType.INVOKE_AGENT_END.value,
                dedupe_key="request.failed",
                lifecycle=RequestStatus.FAILED,
                payload={
                    "request_id": request_id,
                    "agent_id": self.agent_id,
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


def _stream_event(event: RuntimeEvent) -> dict[str, Any]:
    """One stored event's SSE frame with ``seq``, its place in the request's stream.

    A reader that reconnects passes the last ``seq`` it saw as the cursor.
    """
    frame = to_public_event(event)
    frame["seq"] = int(event.request_seq or 0)
    return frame


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

    # Wait for appends before the first read. An event appended while that read
    # is in flight is either in what it returns or wakes the waiter; registered
    # after the read, the waiter would miss it and the stream would sit out the
    # whole wait.
    waiter = self.runtime_store.register_request_waiter(request_id)
    try:
        # One store call for both facts: `done` is scoped to the events it
        # returns, so a caller that stops on `done` has necessarily been given
        # the terminal event. Reading them separately let an append between the
        # two land a `done=True` alongside events truncated before the terminal
        # row, and the stream closed without ever emitting it.
        stored_events, done = await self.runtime_store.read_request_stream(
            request_id,
            after_seq=max(0, int(cursor)),
        )
        if not stored_events and not done and wait_timeout > 0:
            try:
                await asyncio.wait_for(waiter.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                pass
            stored_events, done = await self.runtime_store.read_request_stream(
                request_id,
                after_seq=max(0, int(cursor)),
            )
    finally:
        self.runtime_store.unregister_request_waiter(request_id, waiter)
    next_cursor = (
        int(stored_events[-1].request_seq or 0) if stored_events else int(cursor)
    )
    return [_stream_event(event) for event in stored_events], next_cursor, done


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
        request_metadata = (event.payload or {}).get("request_metadata")
        for key, value in (
            ("app_id", app_id_from_request_metadata(request_metadata)),
            ("policy_id", policy_id_from_request_metadata(request_metadata)),
        ):
            if value is not None:
                data[key] = value
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
            "trace_id": event.trace_id,
            "interaction_id": event.payload.get("interaction_id"),
            "type": event.payload.get("type"),
            "prompt": event.payload.get("prompt"),
            "schema": event.payload.get("schema"),
            "timeout_seconds": event.payload.get("timeout_seconds"),
        }
        # A browser handoff's live-view link and when it lapses, and the child
        # a relayed interaction came from.
        for field_name in ("url", "expires_at", "origin"):
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

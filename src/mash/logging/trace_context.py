"""Async-safe context for trace ID propagation."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_trace_id",
    default=None,
)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_request_id",
    default=None,
)
_host_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_host_id",
    default=None,
)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_session_id",
    default=None,
)
_workflow_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_workflow_id",
    default=None,
)
_workflow_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_workflow_run_id",
    default=None,
)
_request_metadata: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar(
        "mash_request_metadata",
        default=None,
    )
)


def set_trace_id(trace_id: Optional[str]) -> None:
    """Set the trace ID for the current task context."""
    _trace_id.set(trace_id)


def get_trace_id() -> Optional[str]:
    """Get the trace ID for the current task context."""
    return _trace_id.get()


def clear_trace_id() -> None:
    """Clear the trace ID for the current task context."""
    _trace_id.set(None)


def set_request_id(request_id: Optional[str]) -> None:
    """Set the request ID for the current task context."""
    _request_id.set(request_id)


def get_request_id() -> Optional[str]:
    """Get the request ID for the current task context."""
    return _request_id.get()


def clear_request_id() -> None:
    """Clear the request ID for the current task context."""
    _request_id.set(None)


@contextmanager
def bound_trace_id(trace_id: Optional[str]) -> Iterator[None]:
    """Temporarily bind a trace ID for the current task context."""
    token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)


@contextmanager
def bound_request_id(request_id: Optional[str]) -> Iterator[None]:
    """Temporarily bind a request ID for the current task context."""
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def get_session_id() -> Optional[str]:
    """Get the session ID bound for the current task context, if any."""
    return _session_id.get()


def set_session_id(session_id: Optional[str]) -> None:
    """Set the session ID for the current task context."""
    _session_id.set(session_id)


@contextmanager
def bound_session_id(session_id: Optional[str]) -> Iterator[None]:
    """Temporarily bind the session ID for the current task context.

    Lets components that emit events out-of-band (e.g. the MCP manager) tag them
    with the request's session instead of a fixed construction-time value.
    """
    token = _session_id.set(session_id)
    try:
        yield
    finally:
        _session_id.reset(token)


def get_host_id() -> Optional[str]:
    """Get the host (composition) ID for the current task context."""
    return _host_id.get()


@contextmanager
def bound_host_id(host_id: Optional[str]) -> Iterator[None]:
    """Temporarily bind a host (composition) ID for the current task context."""
    token = _host_id.set(host_id)
    try:
        yield
    finally:
        _host_id.reset(token)


def get_request_metadata() -> dict[str, Any]:
    """Get the caller-supplied metadata bound for the current request, if any.

    Returns a copy of the opaque ``metadata`` object the caller attached when
    submitting the request (empty when none was supplied). Tools and other code
    running inside a request can call this to read caller context that never
    reaches the model.
    """
    value = _request_metadata.get()
    return dict(value) if value else {}


@contextmanager
def bound_request_metadata(metadata: Optional[dict[str, Any]]) -> Iterator[None]:
    """Temporarily bind caller-supplied request metadata for the current task."""
    token = _request_metadata.set(dict(metadata) if metadata else None)
    try:
        yield
    finally:
        _request_metadata.reset(token)


def get_workflow_id() -> Optional[str]:
    """Get the workflow ID bound for the current task context, if any."""
    return _workflow_id.get()


def get_workflow_run_id() -> Optional[str]:
    """Get the workflow run ID bound for the current task context, if any."""
    return _workflow_run_id.get()


@contextmanager
def bound_workflow_ids(
    workflow_id: Optional[str], workflow_run_id: Optional[str]
) -> Iterator[None]:
    """Temporarily bind the workflow id + run id for the current task context.

    Lets ``append_runtime_event`` stamp every event of a workflow-issued request
    with its originating workflow, so a run's traces are queryable by run id.
    """
    workflow_token = _workflow_id.set(workflow_id)
    run_token = _workflow_run_id.set(workflow_run_id)
    try:
        yield
    finally:
        _workflow_run_id.reset(run_token)
        _workflow_id.reset(workflow_token)

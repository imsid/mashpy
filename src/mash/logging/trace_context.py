"""Async-safe context for trace ID propagation."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_trace_id",
    default=None,
)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_request_id",
    default=None,
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

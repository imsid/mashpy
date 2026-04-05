"""Async-safe context for trace ID propagation."""

from __future__ import annotations

import contextvars
from typing import Optional

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mash_trace_id",
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

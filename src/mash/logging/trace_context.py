"""Thread-local context for trace ID propagation."""

from __future__ import annotations

import threading
from typing import Optional

# Thread-local storage for trace context
_trace_context = threading.local()


def set_trace_id(trace_id: Optional[str]) -> None:
    """Set the trace ID for the current thread.

    Args:
        trace_id: Trace ID to set, or None to clear.
    """
    _trace_context.trace_id = trace_id


def get_trace_id() -> Optional[str]:
    """Get the trace ID for the current thread.

    Returns:
        Current trace ID, or None if not set.
    """
    return getattr(_trace_context, "trace_id", None)


def clear_trace_id() -> None:
    """Clear the trace ID for the current thread."""
    _trace_context.trace_id = None

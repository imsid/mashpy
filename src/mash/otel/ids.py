"""Trace and span ids derived from stored events.

The same request always projects to the same ids, in the admin UI and in
every export: the trace id is the request's uuid4 trace id with the dashes
removed, and a span id is the first 8 bytes of a hash of the request id and
the span's key (``root``, ``step.3``, ``tool.3.toolu_01``).
"""

from __future__ import annotations

import hashlib
import uuid

INVALID_TRACE_ID = "0" * 32
INVALID_SPAN_ID = "0" * 16


def trace_id_hex(trace_id: str | None) -> str:
    """A Mash trace id (a uuid) as a 32-hex OpenTelemetry trace id."""
    if not trace_id:
        return INVALID_TRACE_ID
    try:
        return uuid.UUID(str(trace_id)).hex
    except ValueError:
        # Not a uuid: derive a stable 128-bit id from it instead.
        return hashlib.sha256(str(trace_id).encode("utf-8")).hexdigest()[:32]


def span_id_hex(scope: str, key: str) -> str:
    """A 16-hex span id for the span ``key`` of ``scope`` (a request or run id)."""
    digest = hashlib.sha256(f"{scope}\x00{key}".encode("utf-8")).hexdigest()[:16]
    # An all-zero span id is invalid; the odds are nil, but stay valid anyway.
    return digest if digest != INVALID_SPAN_ID else "0" * 15 + "1"

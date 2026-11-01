"""W3C trace context (``traceparent``) parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})(?:-.*)?$"
)
_TRACESTATE_MAX = 512


@dataclass(frozen=True)
class TraceParent:
    trace_id: str
    span_id: str
    sampled: bool


def parse_traceparent(value: str | None) -> TraceParent | None:
    """A ``traceparent`` header's ids, or None when it is absent or invalid.

    Follows W3C Trace Context: version ``ff`` and all-zero ids are invalid,
    and a future version may carry extra fields after the flags.
    """
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if match is None:
        return None
    version = match.group("version")
    if version == "ff":
        return None
    if version == "00" and len(value.strip()) != 55:
        return None
    trace_id = match.group("trace_id")
    span_id = match.group("span_id")
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return TraceParent(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(match.group("flags"), 16) & 0x01),
    )


def normalize_trace_context(
    traceparent: str | None, tracestate: str | None
) -> tuple[str | None, str | None]:
    """The trace context to store: a valid ``traceparent`` and its ``tracestate``.

    An invalid ``traceparent`` is dropped along with its ``tracestate``, as the
    spec says; the request then starts a trace of its own.
    """
    if parse_traceparent(traceparent) is None:
        return None, None
    state = (tracestate or "").strip()
    if not state or len(state) > _TRACESTATE_MAX:
        state = ""
    return str(traceparent).strip().lower(), state or None

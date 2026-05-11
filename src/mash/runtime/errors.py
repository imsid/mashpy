"""Helpers for normalizing runtime and provider error payloads."""

from __future__ import annotations

from typing import Any, Dict


def classify_error(error: object) -> Dict[str, Any]:
    """Return structured error fields for runtime payloads."""
    message = str(error or "").strip()
    error_type = error.__class__.__name__ if isinstance(error, BaseException) else None
    if not message:
        message = error_type or "request failed"
    lowered = message.lower()

    error_code = None
    retryable = None
    if "context_length_exceeded" in lowered:
        error_code = "context_length_exceeded"
        retryable = False

    payload: Dict[str, Any] = {"error": message}
    if error_type is not None:
        payload["error_type"] = error_type
    if error_code is not None:
        payload["error_code"] = error_code
    if retryable is not None:
        payload["retryable"] = retryable
    return payload

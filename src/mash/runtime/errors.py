"""Helpers for normalizing runtime and provider error payloads."""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, Sequence, Tuple, TypeVar

_T = TypeVar("_T")

DEFAULT_MAX_STEP_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0

_RETRYABLE_PATTERNS: Sequence[Tuple[Sequence[str], str]] = (
    (("rate_limit", "429", "too many requests"), "rate_limit_exceeded"),
    (("timeout", "timed out", "deadline exceeded"), "timeout"),
    (("connection", "network", "dns", "socket"), "network_error"),
    (
        ("502", "503", "504", "bad gateway", "service unavailable", "gateway timeout"),
        "server_error",
    ),
    (("overloaded", "capacity"), "overloaded"),
)

_TERMINAL_PATTERNS: Sequence[Tuple[Sequence[str], str]] = (
    (("context_length_exceeded",), "context_length_exceeded"),
    (
        ("authentication", "unauthorized", "401", "invalid api key", "invalid_api_key"),
        "auth_error",
    ),
    (("permission", "forbidden", "403"), "permission_denied"),
    (("invalid_request", "400", "bad request", "validation"), "invalid_request"),
    (("not_found", "404"), "not_found"),
)


def classify_error(error: object) -> Dict[str, Any]:
    """Return structured error fields for runtime payloads."""
    message = str(error or "").strip()
    error_type = error.__class__.__name__ if isinstance(error, BaseException) else None
    if not message:
        message = error_type or "request failed"
    lowered = message.lower()

    error_code = None
    retryable = None

    for patterns, code in _RETRYABLE_PATTERNS:
        if any(p in lowered for p in patterns):
            error_code = code
            retryable = True
            break

    if error_code is None:
        for patterns, code in _TERMINAL_PATTERNS:
            if any(p in lowered for p in patterns):
                error_code = code
                retryable = False
                break

    payload: Dict[str, Any] = {"error": message}
    if error_type is not None:
        payload["error_type"] = error_type
    if error_code is not None:
        payload["error_code"] = error_code
    if retryable is not None:
        payload["retryable"] = retryable
    return payload


def is_retryable(error: object) -> bool:
    """Return True if the error is transient and worth retrying.

    Unknown errors (no pattern match) default to retryable — it is safer to
    retry (worst case: fails again) than to permanently abandon work.
    """
    return classify_error(error).get("retryable") is not False


async def retry_transient(
    fn: Callable[[], Awaitable[_T]],
    *,
    max_retries: int = DEFAULT_MAX_STEP_RETRIES,
    base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    max_delay: float = DEFAULT_RETRY_MAX_DELAY,
) -> _T:
    """Call *fn* with automatic retries for transient errors.

    On each retryable failure, sleeps with exponential backoff plus jitter
    before retrying.  Non-retryable errors and exhausted retries propagate
    the exception immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(1 + max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc) or attempt >= max_retries:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            delay *= 0.5 + random.random()  # jitter
            await asyncio.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checkers

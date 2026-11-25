"""Failed turns: user messages whose request failed before the agent replied.

A request the user started that fails saves a turn with the user's message,
no reply, and ``metadata["type"] == "failed_request"`` with the error. Replay
puts it in a ``<failed_request>`` block at the top of the next user message,
so the agent knows what "try again" refers to. Compaction and Cortex leave it
out: it holds no answer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

FAILED_REQUEST_TYPE = "failed_request"
ERROR_CHARS = 300


def is_failed_turn(turn: Dict[str, Any]) -> bool:
    return (turn.get("metadata") or {}).get("type") == FAILED_REQUEST_TYPE


def failed_turn_metadata(error_payload: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata keys that mark a turn as failed, from ``classify_error``'s payload."""
    return {
        "type": FAILED_REQUEST_TYPE,
        "error": {
            "error_code": error_payload.get("error_code"),
            "error": str(error_payload.get("error") or ""),
        },
    }


def format_failed_request(user_message: str, metadata: Optional[Dict[str, Any]]) -> str:
    """The ``<failed_request>`` block replay puts before the next user message."""
    error = (metadata or {}).get("error") or {}
    detail = " ".join(str(error.get("error") or "").split())
    if len(detail) > ERROR_CHARS:
        detail = detail[: ERROR_CHARS - 1] + "…"
    code = error.get("error_code")
    reason = f"{code}: {detail}" if code and detail else str(code or detail or "unknown error")
    return (
        "<failed_request>\n"
        "This user message got no reply: the request failed, recorded by the system.\n"
        f"Error: {reason}\n"
        f"User message: {user_message}\n"
        "</failed_request>"
    )


__all__ = [
    "FAILED_REQUEST_TYPE",
    "failed_turn_metadata",
    "format_failed_request",
    "is_failed_turn",
]

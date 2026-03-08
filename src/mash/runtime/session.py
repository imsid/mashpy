"""Session helpers for cross-agent invocation."""

from __future__ import annotations

import hashlib


def derive_subagent_session_id(
    primary_app_id: str,
    primary_session_id: str,
    subagent_id: str,
) -> str:
    """Derive deterministic subagent session namespace from primary context."""
    key = f"{primary_app_id}:{primary_session_id}:{subagent_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"subagent:{subagent_id}:{digest}"

"""Choosing a session to go on with: the user's sessions as menu rows."""

from __future__ import annotations

import time
from typing import Any

from .render import clip

# How many of the user's sessions the picker offers, most recent first.
PICKER_LIMIT = 20
PICKER_HINT = "↑↓ or a number to move · enter to resume · ctrl-c keeps the current session"


def fmt_ago(seconds: float) -> str:
    """``just now``, ``5m ago``, ``3h ago``, ``2d ago``."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def session_title(session: dict[str, Any]) -> str:
    """A session's first message on one line, or its id when it has none."""
    return clip(str(session.get("first_message") or ""), 60) or str(
        session.get("session_id") or ""
    )


def session_options(
    sessions: list[dict[str, Any]],
    *,
    current: str | None = None,
    now: float | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """One menu row per session: first message, turns, time since last activity."""
    now = time.time() if now is None else now
    options = []
    for session in sessions:
        turns = int(session.get("turn_count") or 0)
        label = (
            f"{session_title(session)} · {turns} turn{'' if turns == 1 else 's'} · "
            f"{fmt_ago(now - float(session.get('last_activity_at') or 0.0))}"
        )
        if current is not None and session.get("session_id") == current:
            label += " (current)"
        options.append((session, label))
    return options

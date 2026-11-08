"""Session reads over the memory store and runtime event log.

These read Postgres only, so the runtime and the host share them: the runtime
for its own agent, the host for any agent that uses the pool's shared memory
store.
"""

from __future__ import annotations

from typing import Any

from ..memory.store import MemoryStore
from .events import RuntimeStore


async def session_event_token_totals(
    runtime_store: RuntimeStore | None, session_id: str
) -> dict[str, int]:
    """Tokens a session's requests used, across every agent."""
    if runtime_store is None:
        return {"total_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    return await runtime_store.session_usage(session_id)


async def session_total_tokens(store: MemoryStore, *, app_id: str, session_id: str) -> int:
    turns = await store.get_turns(session_id=session_id, app_id=app_id, limit=1)
    if not turns:
        return 0
    value = turns[-1].get("session_total_tokens", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def session_info(
    store: MemoryStore,
    runtime_store: RuntimeStore | None,
    *,
    app_id: str,
    session_id: str,
    model: Any,
    max_steps: Any,
) -> dict[str, Any]:
    # total_tokens is the true spend recorded in the event log across every agent
    # in the session (matching the admin rollup); session_total_tokens is the
    # memory context budget, which compaction resets.
    event_totals = await session_event_token_totals(runtime_store, session_id)
    return {
        "app_id": app_id,
        "agent_id": app_id,
        "session_id": session_id,
        "model": model,
        "max_steps": max_steps,
        "session_total_tokens": await session_total_tokens(
            store, app_id=app_id, session_id=session_id
        ),
        "total_tokens": event_totals["total_tokens"],
        "cache_read_tokens": event_totals["cache_read_tokens"],
        "cache_write_tokens": event_totals["cache_write_tokens"],
    }


async def history_turns(
    store: MemoryStore, *, app_id: str, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    return await store.get_turns(session_id=session_id, app_id=app_id, limit=limit)


async def list_sessions(store: MemoryStore, *, app_id: str) -> list[dict[str, Any]]:
    if not hasattr(store, "list_sessions"):
        return []
    return await store.list_sessions(app_id=app_id)


async def session_signals(
    store: MemoryStore, *, app_id: str, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    return await store.get_session_signals(
        session_id=session_id, app_id=app_id, limit=limit
    )

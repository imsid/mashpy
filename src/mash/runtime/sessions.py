"""Session reads over the memory store and runtime event log.

These read Postgres only, so the runtime and the host share them: the runtime
for its own agent, the host for any agent that uses the pool's shared memory
store.
"""

from __future__ import annotations

from typing import Any

from ..memory.store import MemoryStore
from .events import RuntimeStore
from .events.types import usage_totals


async def session_usage(
    runtime_store: RuntimeStore | None, session_id: str
) -> dict[str, Any]:
    """Tokens a session used, its subagent runs included, by agent."""
    if runtime_store is None:
        return usage_totals([])
    return await runtime_store.session_usage(session_id)


async def session_total_tokens(store: MemoryStore, *, agent_id: str, session_id: str) -> int:
    turns = await store.get_turns(session_id=session_id, agent_id=agent_id, limit=1)
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
    agent_id: str,
    session_id: str,
    model: Any,
    max_steps: Any,
) -> dict[str, Any]:
    # total_tokens is the true spend recorded in the event log: every agent's
    # requests in the session and the subagent runs they started, in sessions
    # of their own. session_total_tokens is the memory context budget, which
    # compaction resets.
    usage = await session_usage(runtime_store, session_id)
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "model": model,
        "max_steps": max_steps,
        "session_total_tokens": await session_total_tokens(
            store, agent_id=agent_id, session_id=session_id
        ),
        "total_tokens": usage["total_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "usage": usage,
    }


async def history_turns(
    store: MemoryStore, *, agent_id: str, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    return await store.get_turns(session_id=session_id, agent_id=agent_id, limit=limit)


async def list_sessions(
    store: MemoryStore, *, agent_id: str, limit: int, app_id: str | None = None
) -> list[dict[str, Any]]:
    return await store.list_sessions(agent_id=agent_id, limit=limit, app_id=app_id)


async def session_signals(
    store: MemoryStore, *, agent_id: str, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    return await store.get_session_signals(
        session_id=session_id, agent_id=agent_id, limit=limit
    )

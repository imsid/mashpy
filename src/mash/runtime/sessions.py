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


def _is_companion(agent_id: str) -> bool:
    from mash.agents.companion import COMPANION_AGENT_ID

    return agent_id == COMPANION_AGENT_ID


async def context_tokens(store: MemoryStore, *, agent_id: str, session_id: str) -> int:
    """The latest turn's context size: its last model call's input, cached included, plus output.

    On the companion the session is a conversation, and its size is the board's.
    """
    if _is_companion(agent_id):
        last = await store.last_entry(session_id)
        return int((last or {}).get("board_tokens") or 0)
    turns = await store.get_turns(session_id=session_id, agent_id=agent_id, limit=1)
    if not turns:
        return 0
    value = turns[-1].get("context_tokens", 0)
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
    # of their own. context_tokens is the size of the latest turn's context,
    # which the session compacts on.
    usage = await session_usage(runtime_store, session_id)
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "model": model,
        "max_steps": max_steps,
        "context_tokens": await context_tokens(
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
    """The session's turns, oldest first: the last ``limit`` when given.

    On the companion the session is a conversation, and its turns are the
    board's entries, whichever agent wrote them.
    """
    if not _is_companion(agent_id):
        return await store.get_turns(session_id=session_id, agent_id=agent_id, limit=limit)
    if limit is None:
        entries = await store.read_entries(session_id)
    else:
        last = await store.last_entry(session_id)
        entries = (
            []
            if last is None
            else await store.read_entries(
                session_id, before=int(last["seq"]) + 1, limit=max(0, int(limit))
            )
        )
    return [
        {
            **entry,
            "context_tokens": entry["board_tokens"],
            "replayable": True,
            "signals": {},
        }
        for entry in entries
    ]


async def list_sessions(
    store: MemoryStore, *, agent_id: str, limit: int, app_id: str | None = None
) -> list[dict[str, Any]]:
    """The agent's most recently active sessions; on the companion, its conversations."""
    if _is_companion(agent_id):
        return await store.list_board_sessions(limit=limit, app_id=app_id)
    return await store.list_sessions(agent_id=agent_id, limit=limit, app_id=app_id)


async def session_signals(
    store: MemoryStore, *, agent_id: str, session_id: str, limit: int | None = None
) -> list[dict[str, Any]]:
    return await store.get_session_signals(
        session_id=session_id, agent_id=agent_id, limit=limit
    )

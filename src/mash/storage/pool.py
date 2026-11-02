"""One Postgres connection pool per process and database URL.

Every Postgres store in a process borrows the same pool: the first store to
open it creates it and runs the migrations, the last to close it closes it.
The pool keeps no connections while idle: ``min_size=0``, and a connection
unused for ``MAX_IDLE_SECONDS`` is closed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # pragma: no cover - env dependent
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]

from .migrations import run_migrations

MAX_SIZE = 20
MAX_IDLE_SECONDS = 60.0


@dataclass
class _Shared:
    pool: Any
    users: int


_POOLS: dict[str, _Shared] = {}
_LOCK = asyncio.Lock()


async def acquire_pool(database_url: str) -> Any:
    """The process's pool for ``database_url``; pair with ``release_pool``."""
    if dict_row is None or AsyncConnectionPool is None:
        raise RuntimeError(
            "psycopg and psycopg_pool are required for the Postgres stores. "
            "Install mashpy with PostgreSQL dependencies."
        )
    async with _LOCK:
        shared = _POOLS.get(database_url)
        if shared is None:
            pool = AsyncConnectionPool(
                database_url,
                min_size=0,
                max_size=MAX_SIZE,
                max_idle=MAX_IDLE_SECONDS,
                open=False,
                # Managed Postgres closes idle connections server-side; validate
                # at checkout so a stale connection is replaced, not handed out.
                check=AsyncConnectionPool.check_connection,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await pool.open()
            try:
                await run_migrations(pool)
            except BaseException:
                await pool.close()
                raise
            shared = _Shared(pool=pool, users=0)
            _POOLS[database_url] = shared
        shared.users += 1
        return shared.pool


async def release_pool(database_url: str) -> None:
    async with _LOCK:
        shared = _POOLS.get(database_url)
        if shared is None:
            return
        shared.users -= 1
        if shared.users > 0:
            return
        del _POOLS[database_url]
        await shared.pool.close()


__all__ = ["acquire_pool", "release_pool"]

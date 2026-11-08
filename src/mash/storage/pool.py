"""One Postgres connection pool per process and database URL.

Every Postgres store in a process borrows the same pool: the first store to
open it creates it and runs the migrations, the last to close it closes it.
The pool keeps no connections while idle: ``min_size=0``, and a connection
unused for ``MAX_IDLE_SECONDS`` is closed. It holds at most
``MASH_DB_POOL_MAX_SIZE`` connections (default 10); the host and each agent
process have a pool of their own.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # pragma: no cover - env dependent
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]

from .migrations import run_migrations

MASH_DB_POOL_MAX_SIZE_ENV = "MASH_DB_POOL_MAX_SIZE"
DEFAULT_MAX_SIZE = 10
MAX_IDLE_SECONDS = 60.0

logger = logging.getLogger(__name__)


def pool_max_size() -> int:
    """The most connections one process's pool holds, from ``MASH_DB_POOL_MAX_SIZE``."""
    raw = (os.environ.get(MASH_DB_POOL_MAX_SIZE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_SIZE
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(
            f"{MASH_DB_POOL_MAX_SIZE_ENV} must be a positive integer, got {raw!r}"
        )
    return value


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
    max_size = pool_max_size()
    async with _LOCK:
        shared = _POOLS.get(database_url)
        if shared is None:
            pool = AsyncConnectionPool(
                database_url,
                min_size=0,
                max_size=max_size,
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


async def check_connection_budget(pool: Any, *, processes: int) -> None:
    """Warn when ``processes`` full pools could open more than the server allows.

    The host calls this once at startup with itself plus one per agent
    process. ``max_connections`` also covers superuser and other clients, so
    this is a floor, not a guarantee.
    """
    budget = processes * pool_max_size()
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SHOW max_connections")
            row = await cursor.fetchone()
    limit = int(row["max_connections"]) if row is not None else 0
    if limit and budget > limit:
        logger.warning(
            "%d processes with %s=%d can open %d Postgres connections, above "
            "the server's max_connections of %d; lower %s or raise max_connections",
            processes,
            MASH_DB_POOL_MAX_SIZE_ENV,
            pool_max_size(),
            budget,
            limit,
            MASH_DB_POOL_MAX_SIZE_ENV,
        )


__all__ = [
    "DEFAULT_MAX_SIZE",
    "MASH_DB_POOL_MAX_SIZE_ENV",
    "acquire_pool",
    "check_connection_budget",
    "pool_max_size",
    "release_pool",
]

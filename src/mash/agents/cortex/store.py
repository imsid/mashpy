"""CortexStore: the ``cortex_user`` and ``cortex_entry`` tables.

Agents read Cortex on their computers through this store, in the runtime's
``mash`` process. Bash runs as ``agent`` and cannot reach the tables.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mash.storage.migrations import run_migrations

from .models import CortexEntry

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]


# Recognized in-memory sentinel URL, mirroring PostgresEvalStore. Tests that
# use this URL get an in-process store and never open a real connection.
_TEST_DATABASE_URL = "postgresql://test/runtime"


class _InMemoryCortexStore:
    """In-memory backend for the ``postgresql://test/runtime`` sentinel."""

    def __init__(self) -> None:
        self.profiles: dict[str, str] = {}
        self.entries: dict[str, CortexEntry] = {}

    async def get_profile(self, user_id: str) -> str | None:
        return self.profiles.get(user_id)

    async def search_entries(
        self, user_id: str, query: str, *, limit: int
    ) -> list[CortexEntry]:
        terms = [term.lower() for term in query.split() if term]
        matches = [
            entry
            for entry in self.entries.values()
            if entry.user_id == user_id
            and all(term in f"{entry.topic} {entry.content}".lower() for term in terms)
        ]
        matches.sort(key=lambda entry: entry.updated_at, reverse=True)
        return matches[:limit]


class CortexStore:
    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._mem: _InMemoryCortexStore | None = (
            _InMemoryCortexStore() if resolved == _TEST_DATABASE_URL else None
        )

    async def open(self) -> None:
        if self._mem is not None or self._pool is not None:
            return
        if dict_row is None or AsyncConnectionPool is None:
            raise RuntimeError("psycopg and psycopg_pool are required for CortexStore.")
        async with self._open_lock:
            if self._pool is not None:
                return
            pool = AsyncConnectionPool(
                self._database_url,
                min_size=1,
                max_size=5,
                open=False,
                check=AsyncConnectionPool.check_connection,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await pool.open()
            await run_migrations(pool)
            self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get_profile(self, user_id: str) -> str | None:
        """The user's profile, or None when the user has no Cortex."""
        if self._mem is not None:
            return await self._mem.get_profile(user_id)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT profile FROM cortex_user WHERE user_id = %s", (user_id,)
                )
                row = await cursor.fetchone()
        return str(row["profile"]) if row is not None else None

    async def search_entries(
        self, user_id: str, query: str, *, limit: int = 10
    ) -> list[CortexEntry]:
        """The user's entries matching every term of ``query``, best first."""
        normalized = " ".join(term for term in str(query).split() if term)
        if not normalized or limit <= 0:
            return []
        if self._mem is not None:
            return await self._mem.search_entries(user_id, normalized, limit=limit)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT entry_id, user_id, topic, content, origin, sources,
                           created_at, updated_at
                    FROM cortex_entry
                    WHERE user_id = %s
                      AND to_tsvector('simple', topic || ' ' || content)
                          @@ plainto_tsquery('simple', %s)
                    ORDER BY ts_rank_cd(
                                 to_tsvector('simple', topic || ' ' || content),
                                 plainto_tsquery('simple', %s)
                             ) DESC,
                             updated_at DESC
                    LIMIT %s
                    """,
                    (user_id, normalized, normalized, int(limit)),
                )
                rows = await cursor.fetchall()
        return [_entry_from_row(row) for row in rows]


def _entry_from_row(row: dict[str, Any]) -> CortexEntry:
    sources = row["sources"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    return CortexEntry(
        entry_id=str(row["entry_id"]),
        user_id=str(row["user_id"]),
        topic=str(row["topic"]),
        content=str(row["content"]),
        origin=str(row["origin"]),
        sources=list(sources or []),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


__all__ = ["CortexStore"]

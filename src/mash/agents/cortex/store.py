"""CortexStore: the ``cortex_user`` and ``cortex_entry`` tables.

Agents read Cortex on their computers through this store, in the runtime's
``mash`` process. Bash runs as ``agent`` and cannot reach the tables.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import replace
from typing import Any

from mash.storage.migrations import run_migrations

from .models import (
    CortexConflictError,
    CortexEntry,
    CortexSnapshot,
    CortexUserInfo,
    EntryChange,
)

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
        # user_id -> {"built_through", "version", "last_build"}
        self.users: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

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

    async def load(self, user_id: str) -> CortexSnapshot:
        if user_id not in self.profiles:
            return CortexSnapshot(user_id=user_id)
        state = self.users.get(user_id) or {}
        entries = sorted(
            (e for e in self.entries.values() if e.user_id == user_id),
            key=lambda e: e.updated_at,
            reverse=True,
        )
        return CortexSnapshot(
            user_id=user_id,
            profile=self.profiles[user_id],
            built_through=float(state.get("built_through", 0.0)),
            version=int(state.get("version", 0)),
            entries=list(entries),
        )

    async def apply_build(self, **kwargs: Any) -> dict[str, Any]:
        async with self._lock:
            user_id = kwargs["user_id"]
            state = self.users.get(user_id)
            if state is not None and (state.get("last_build") or {}).get("run_id") == kwargs["run_id"]:
                return dict(state["last_build"]["result"])
            current = int(state["version"]) if state is not None else 0
            if current != kwargs["base_version"]:
                raise CortexConflictError(
                    f"cortex for user '{user_id}' changed since the build read it"
                )
            now = time.time()
            origins = {e.entry_id: e.origin for e in self.entries.values() if e.user_id == user_id}
            written = 0
            for change in kwargs["upserts"]:
                origin = origins.get(change.entry_id) if change.entry_id else None
                if origin == "app":
                    continue
                if origin == "build":
                    self.entries[change.entry_id] = replace(
                        self.entries[change.entry_id],
                        topic=change.topic,
                        content=change.content,
                        sources=list(change.sources),
                        updated_at=now,
                    )
                else:
                    entry_id = uuid.uuid4().hex
                    self.entries[entry_id] = CortexEntry(
                        entry_id=entry_id,
                        user_id=user_id,
                        topic=change.topic,
                        content=change.content,
                        origin="build",
                        sources=list(change.sources),
                        created_at=now,
                        updated_at=now,
                    )
                written += 1
            deleted = 0
            for entry_id in kwargs["deletes"]:
                if origins.get(entry_id) == "build" and entry_id in self.entries:
                    del self.entries[entry_id]
                    deleted += 1
            result = {**kwargs["result"], "entries_written": written, "entries_deleted": deleted}
            self.profiles[user_id] = kwargs["profile"]
            self.users[user_id] = {
                "built_through": kwargs["built_through"],
                "version": current + 1,
                "last_build": {"run_id": kwargs["run_id"], "result": result},
            }
            return dict(result)

    # App API ------------------------------------------------------------

    def _bump(self, user_id: str, now: float) -> int:
        state = self.users.setdefault(user_id, {"built_through": 0.0, "version": 0})
        state["version"] = int(state.get("version", 0)) + 1
        state["updated_at"] = now
        self.profiles.setdefault(user_id, "")
        return state["version"]

    def _ordered(self, user_id: str) -> list[CortexEntry]:
        return sorted(
            (e for e in self.entries.values() if e.user_id == user_id),
            key=lambda e: (e.updated_at, e.entry_id),
            reverse=True,
        )

    async def get_user(self, user_id: str) -> CortexUserInfo | None:
        if user_id not in self.profiles:
            return None
        state = self.users.get(user_id) or {}
        return CortexUserInfo(
            user_id=user_id,
            profile=self.profiles[user_id],
            built_through=float(state.get("built_through", 0.0)),
            version=int(state.get("version", 0)),
            entry_count=len(self._ordered(user_id)),
            updated_at=float(state.get("updated_at", 0.0)),
        )

    async def list_entries(self, user_id: str, **kwargs: Any) -> list[CortexEntry]:
        rows = self._ordered(user_id)
        if kwargs.get("origin"):
            rows = [e for e in rows if e.origin == kwargs["origin"]]
        terms = [t.lower() for t in str(kwargs.get("q") or "").split() if t]
        if terms:
            rows = [e for e in rows if all(t in f"{e.topic} {e.content}".lower() for t in terms)]
        after = kwargs.get("after")
        if after:
            ids = [e.entry_id for e in rows]
            rows = rows[ids.index(after) + 1 :] if after in ids else []
        return rows[: kwargs["limit"]]

    async def create_entry(self, user_id: str, *, topic: str, content: str) -> tuple[CortexEntry, int]:
        async with self._lock:
            now = time.time()
            entry = CortexEntry(
                entry_id=uuid.uuid4().hex, user_id=user_id, topic=topic, content=content,
                origin="app", created_at=now, updated_at=now,
            )
            self.entries[entry.entry_id] = entry
            return entry, self._bump(user_id, now)

    async def update_entry(
        self, user_id: str, entry_id: str, *, topic: str | None, content: str | None
    ) -> tuple[CortexEntry, int] | None:
        async with self._lock:
            entry = self.entries.get(entry_id)
            if entry is None or entry.user_id != user_id:
                return None
            now = time.time()
            entry = replace(
                entry,
                topic=entry.topic if topic is None else topic,
                content=entry.content if content is None else content,
                origin="app",
                updated_at=now,
            )
            self.entries[entry_id] = entry
            return entry, self._bump(user_id, now)

    async def delete_entry(self, user_id: str, entry_id: str) -> int | None:
        async with self._lock:
            entry = self.entries.get(entry_id)
            if entry is None or entry.user_id != user_id:
                return None
            del self.entries[entry_id]
            return self._bump(user_id, time.time())

    async def delete_user(self, user_id: str) -> bool:
        async with self._lock:
            if user_id not in self.profiles:
                return False
            del self.profiles[user_id]
            self.users.pop(user_id, None)
            for entry_id in [e.entry_id for e in self.entries.values() if e.user_id == user_id]:
                del self.entries[entry_id]
            return True


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


    async def load(self, user_id: str) -> CortexSnapshot:
        """The user's profile, watermark, version and entries, newest first."""
        if self._mem is not None:
            return await self._mem.load(user_id)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT profile, built_through, version
                    FROM cortex_user WHERE user_id = %s
                    """,
                    (user_id,),
                )
                user = await cursor.fetchone()
                if user is None:
                    return CortexSnapshot(user_id=user_id)
                await cursor.execute(
                    f"""
                    SELECT {_ENTRY_COLUMNS} FROM cortex_entry
                    WHERE user_id = %s ORDER BY updated_at DESC
                    """,
                    (user_id,),
                )
                rows = await cursor.fetchall()
        return CortexSnapshot(
            user_id=user_id,
            profile=str(user["profile"]),
            built_through=float(user["built_through"]),
            version=int(user["version"]),
            entries=[_entry_from_row(row) for row in rows],
        )

    async def apply_build(
        self,
        *,
        user_id: str,
        run_id: str,
        base_version: int,
        built_through: float,
        profile: str,
        upserts: list[EntryChange],
        deletes: list[str],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one build in one transaction, if nothing wrote since it read.

        A compare-and-set on ``base_version``: another build or an app edit in
        between raises ``CortexConflictError``. Pinned ``app`` entries are never
        changed or deleted; an upsert naming an unknown entry adds a new one.
        Returns ``result`` with ``entries_written`` and ``entries_deleted``. A
        retry of a run that already applied returns its stored result.
        """
        kwargs = dict(
            user_id=user_id,
            run_id=run_id,
            base_version=base_version,
            built_through=built_through,
            profile=profile,
            upserts=upserts,
            deletes=deletes,
            result=result,
        )
        if self._mem is not None:
            return await self._mem.apply_build(**kwargs)
        await self.open()
        now = time.time()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO cortex_user (user_id, updated_at) VALUES (%s, %s) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (user_id, now),
                    )
                    await cursor.execute(
                        "SELECT version, last_build FROM cortex_user "
                        "WHERE user_id = %s FOR UPDATE",
                        (user_id,),
                    )
                    row = await cursor.fetchone()
                    last_build = row["last_build"] or {}
                    if isinstance(last_build, str):
                        last_build = json.loads(last_build)
                    if last_build.get("run_id") == run_id:
                        return dict(last_build["result"])
                    if int(row["version"]) != base_version:
                        raise CortexConflictError(
                            f"cortex for user '{user_id}' changed since the build read it"
                        )
                    await cursor.execute(
                        "SELECT entry_id, origin FROM cortex_entry WHERE user_id = %s",
                        (user_id,),
                    )
                    origins = {
                        str(r["entry_id"]): str(r["origin"]) for r in await cursor.fetchall()
                    }
                    written = 0
                    for change in upserts:
                        origin = origins.get(change.entry_id) if change.entry_id else None
                        if origin == "app":
                            continue
                        sources = json.dumps(list(change.sources))
                        if origin == "build":
                            await cursor.execute(
                                """
                                UPDATE cortex_entry
                                SET topic = %s, content = %s, sources = %s::jsonb, updated_at = %s
                                WHERE entry_id = %s
                                """,
                                (change.topic, change.content, sources, now, change.entry_id),
                            )
                        else:
                            await cursor.execute(
                                """
                                INSERT INTO cortex_entry
                                    (entry_id, user_id, topic, content, origin, sources,
                                     created_at, updated_at)
                                VALUES (%s, %s, %s, %s, 'build', %s::jsonb, %s, %s)
                                """,
                                (
                                    uuid.uuid4().hex,
                                    user_id,
                                    change.topic,
                                    change.content,
                                    sources,
                                    now,
                                    now,
                                ),
                            )
                        written += 1
                    build_deletes = [
                        entry_id for entry_id in deletes if origins.get(entry_id) == "build"
                    ]
                    if build_deletes:
                        await cursor.execute(
                            "DELETE FROM cortex_entry WHERE user_id = %s AND entry_id = ANY(%s)",
                            (user_id, build_deletes),
                        )
                    applied = {
                        **result,
                        "entries_written": written,
                        "entries_deleted": len(build_deletes),
                    }
                    await cursor.execute(
                        """
                        UPDATE cortex_user
                        SET profile = %s, built_through = %s, version = version + 1,
                            updated_at = %s, last_build = %s::jsonb
                        WHERE user_id = %s
                        """,
                        (
                            profile,
                            float(built_through),
                            now,
                            json.dumps({"run_id": run_id, "result": applied}),
                            user_id,
                        ),
                    )
        return applied


    # App API ------------------------------------------------------------

    async def get_user(self, user_id: str) -> CortexUserInfo | None:
        """The user's profile, watermark, version and entry count, or None."""
        if self._mem is not None:
            return await self._mem.get_user(user_id)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT u.profile, u.built_through, u.version, u.updated_at,
                           (SELECT COUNT(*) FROM cortex_entry e WHERE e.user_id = u.user_id)
                               AS entry_count
                    FROM cortex_user u WHERE u.user_id = %s
                    """,
                    (user_id,),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return CortexUserInfo(
            user_id=user_id,
            profile=str(row["profile"]),
            built_through=float(row["built_through"]),
            version=int(row["version"]),
            entry_count=int(row["entry_count"]),
            updated_at=float(row["updated_at"]),
        )

    async def list_entries(
        self,
        user_id: str,
        *,
        q: str | None = None,
        origin: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> list[CortexEntry]:
        """The user's entries, newest first. ``after`` is the last entry id of
        the previous page; ``q`` matches every term in topic and content."""
        if self._mem is not None:
            return await self._mem.list_entries(
                user_id, q=q, origin=origin, limit=limit, after=after
            )
        await self.open()
        filters = ["user_id = %s"]
        params: list[Any] = [user_id]
        if origin:
            filters.append("origin = %s")
            params.append(origin)
        terms = " ".join(term for term in str(q or "").split() if term)
        if terms:
            filters.append(
                "to_tsvector('simple', topic || ' ' || content) @@ plainto_tsquery('simple', %s)"
            )
            params.append(terms)
        if after:
            filters.append(
                "(updated_at, entry_id) < (SELECT updated_at, entry_id FROM cortex_entry "
                "WHERE entry_id = %s AND user_id = %s)"
            )
            params.extend([after, user_id])
        params.append(int(limit))
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT {_ENTRY_COLUMNS} FROM cortex_entry
                    WHERE {' AND '.join(filters)}
                    ORDER BY updated_at DESC, entry_id DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = await cursor.fetchall()
        return [_entry_from_row(row) for row in rows]

    async def _bump(self, cursor: Any, user_id: str, now: float) -> int:
        await cursor.execute(
            """
            UPDATE cortex_user SET version = version + 1, updated_at = %s
            WHERE user_id = %s RETURNING version
            """,
            (now, user_id),
        )
        return int((await cursor.fetchone())["version"])

    async def create_entry(
        self, user_id: str, *, topic: str, content: str
    ) -> tuple[CortexEntry, int]:
        """Add a pinned ``app`` entry, creating the user's row when missing.
        Returns the entry and the user's new version."""
        if self._mem is not None:
            return await self._mem.create_entry(user_id, topic=topic, content=content)
        await self.open()
        now = time.time()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO cortex_user (user_id, updated_at) VALUES (%s, %s) "
                        "ON CONFLICT (user_id) DO NOTHING",
                        (user_id, now),
                    )
                    await cursor.execute(
                        f"""
                        INSERT INTO cortex_entry
                            (entry_id, user_id, topic, content, origin, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, 'app', %s, %s)
                        RETURNING {_ENTRY_COLUMNS}
                        """,
                        (uuid.uuid4().hex, user_id, topic, content, now, now),
                    )
                    entry = _entry_from_row(await cursor.fetchone())
                    return entry, await self._bump(cursor, user_id, now)

    async def update_entry(
        self,
        user_id: str,
        entry_id: str,
        *,
        topic: str | None = None,
        content: str | None = None,
    ) -> tuple[CortexEntry, int] | None:
        """Change an entry's topic or content; it becomes a pinned ``app`` entry.
        Returns the entry and the new version, or None for an unknown entry."""
        if self._mem is not None:
            return await self._mem.update_entry(user_id, entry_id, topic=topic, content=content)
        await self.open()
        now = time.time()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"""
                        UPDATE cortex_entry
                        SET topic = COALESCE(%s, topic), content = COALESCE(%s, content),
                            origin = 'app', updated_at = %s
                        WHERE user_id = %s AND entry_id = %s
                        RETURNING {_ENTRY_COLUMNS}
                        """,
                        (topic, content, now, user_id, entry_id),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    return _entry_from_row(row), await self._bump(cursor, user_id, now)

    async def delete_entry(self, user_id: str, entry_id: str) -> int | None:
        """Delete one entry. Returns the new version, or None for an unknown entry."""
        if self._mem is not None:
            return await self._mem.delete_entry(user_id, entry_id)
        await self.open()
        now = time.time()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM cortex_entry WHERE user_id = %s AND entry_id = %s",
                        (user_id, entry_id),
                    )
                    if cursor.rowcount == 0:
                        return None
                    return await self._bump(cursor, user_id, now)

    async def delete_user(self, user_id: str) -> bool:
        """Delete the user's whole Cortex. The next build starts from their first turn."""
        if self._mem is not None:
            return await self._mem.delete_user(user_id)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("DELETE FROM cortex_user WHERE user_id = %s", (user_id,))
                return cursor.rowcount > 0


_ENTRY_COLUMNS = (
    "entry_id, user_id, topic, content, origin, sources, created_at, updated_at"
)


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

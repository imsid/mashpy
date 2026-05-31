"""Postgres-backed canonical runtime event store."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Protocol, cast

from .types import RuntimeEvent, RuntimeEventType

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    psycopg = None
    dict_row = None
    AsyncConnectionPool = None


class RuntimeStore(Protocol):
    """Append-only runtime event store."""

    async def open(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        ...

    async def list_request_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]:
        ...

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        ...

    async def has_request(self, request_id: str) -> bool:
        ...

    async def is_request_terminal(self, request_id: str) -> bool:
        ...

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        ...

    async def list_recent_traces(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        ...

    def register_request_waiter(self, request_id: str) -> asyncio.Event:
        ...

    def unregister_request_waiter(
        self, request_id: str, event: asyncio.Event
    ) -> None:
        ...

    def register_global_waiter(self) -> asyncio.Event:
        ...

    def unregister_global_waiter(self, event: asyncio.Event) -> None:
        ...


class PostgresRuntimeStore(RuntimeStore):
    """Append-only Postgres-backed runtime event store."""

    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._request_waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._global_waiters: set[asyncio.Event] = set()
        self._listener_conn: Any = None
        self._listener_task: asyncio.Task | None = None

    async def open(self) -> None:
        if self._pool is not None:
            return
        if psycopg is None or dict_row is None or AsyncConnectionPool is None:
            raise RuntimeError(
                "psycopg and psycopg_pool are required for PostgresRuntimeStore. "
                "Install mashpy with PostgreSQL runtime dependencies."
            )
        async with self._open_lock:
            if self._pool is not None:
                return
            pool = AsyncConnectionPool(
                self._database_url,
                min_size=2,
                max_size=10,
                open=False,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await pool.open()
            self._pool = pool
            await self._init_schema()

            self._listener_conn = cast(
                Any,
                await psycopg.AsyncConnection.connect(
                    self._database_url, autocommit=True
                ),
            )
            await self._listener_conn.execute("LISTEN runtime_events")
            self._listener_task = asyncio.create_task(self._listen_loop())

    async def close(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._listener_conn is not None:
            await self._listener_conn.close()
            self._listener_conn = None

        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _listen_loop(self) -> None:
        try:
            async for notify in self._listener_conn.notifies():
                request_id = notify.payload
                if request_id:
                    for ev in self._request_waiters.get(request_id, ()):
                        ev.set()
                for ev in self._global_waiters:
                    ev.set()
        except asyncio.CancelledError:
            return

    def register_request_waiter(self, request_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._request_waiters[request_id].add(event)
        return event

    def unregister_request_waiter(
        self, request_id: str, event: asyncio.Event
    ) -> None:
        waiters = self._request_waiters.get(request_id)
        if waiters:
            waiters.discard(event)
            if not waiters:
                del self._request_waiters[request_id]

    def register_global_waiter(self) -> asyncio.Event:
        event = asyncio.Event()
        self._global_waiters.add(event)
        return event

    def unregister_global_waiter(self, event: asyncio.Event) -> None:
        self._global_waiters.discard(event)

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    if event.request_id and event.dedupe_key:
                        await cursor.execute(
                            """
                            SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                                   agent_id, session_id, event_type, loop_index, step_key,
                                   dedupe_key, payload, created_at
                            FROM runtime_event_log
                            WHERE request_id = %s AND dedupe_key = %s
                            LIMIT 1
                            """,
                            (event.request_id, event.dedupe_key),
                        )
                        existing = await cursor.fetchone()
                        if existing is not None:
                            return self._dict_to_event(existing)

                    next_request_seq: int | None = None
                    if event.request_id:
                        await cursor.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (event.request_id,),
                        )
                        await cursor.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0) + 1 AS next_request_seq
                            FROM runtime_event_log
                            WHERE request_id = %s
                            """,
                            (event.request_id,),
                        )
                        row = await cursor.fetchone()
                        next_request_seq = (
                            int(row["next_request_seq"]) if row is not None else 1
                        )

                    await cursor.execute(
                        """
                        INSERT INTO runtime_event_log (
                            request_id, trace_id, app_id, agent_id, session_id, seq,
                            event_type, loop_index, step_key, dedupe_key, payload, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        RETURNING event_id, request_id, seq AS request_seq, trace_id, app_id,
                                  agent_id, session_id, event_type, loop_index, step_key,
                                  dedupe_key, payload, created_at
                        """,
                        (
                            event.request_id,
                            event.trace_id,
                            event.app_id,
                            event.agent_id,
                            event.session_id,
                            next_request_seq,
                            event.event_type,
                            event.loop_index,
                            event.step_key,
                            event.dedupe_key,
                            json.dumps(event.payload or {}, ensure_ascii=True, default=str),
                            float(event.created_at),
                        ),
                    )
                    stored = await cursor.fetchone()
                    if stored is None:
                        raise RuntimeError("failed to persist runtime event")
                    await cursor.execute(
                        "SELECT pg_notify('runtime_events', %s)",
                        (event.request_id or "",),
                    )
        return self._dict_to_event(stored)

    async def list_request_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                           agent_id, session_id, event_type, loop_index, step_key,
                           dedupe_key, payload, created_at
                    FROM runtime_event_log
                    WHERE request_id = %s AND seq > %s
                    ORDER BY seq ASC
                    """,
                    (request_id, int(after_seq)),
                )
                rows = await cursor.fetchall()
        return [self._dict_to_event(row) for row in rows]

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        await self.open()
        clauses = ["app_id = %s", "event_id > %s"]
        params: list[Any] = [app_id, int(after_event_id)]
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        if trace_id is not None:
            clauses.append("trace_id = %s")
            params.append(trace_id)
        if limit is not None:
            query = f"""
                SELECT event_id, request_id, request_seq, trace_id, app_id,
                       agent_id, session_id, event_type, loop_index, step_key,
                       dedupe_key, payload, created_at
                FROM (
                    SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                           agent_id, session_id, event_type, loop_index, step_key,
                           dedupe_key, payload, created_at
                    FROM runtime_event_log
                    WHERE {' AND '.join(clauses)}
                    ORDER BY event_id DESC
                    LIMIT %s
                ) AS recent_events
                ORDER BY event_id ASC
            """
            params.append(max(1, int(limit)))
        else:
            query = f"""
                SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                       agent_id, session_id, event_type, loop_index, step_key,
                       dedupe_key, payload, created_at
                FROM runtime_event_log
                WHERE {' AND '.join(clauses)}
                ORDER BY event_id ASC
            """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [self._dict_to_event(row) for row in rows]

    async def has_request(self, request_id: str) -> bool:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT 1
                    FROM runtime_event_log
                    WHERE request_id = %s
                    LIMIT 1
                    """,
                    (request_id,),
                )
                row = await cursor.fetchone()
        return row is not None

    async def is_request_terminal(self, request_id: str) -> bool:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT event_type
                    FROM runtime_event_log
                    WHERE request_id = %s
                    ORDER BY seq DESC
                    LIMIT 1
                    """,
                    (request_id,),
                )
                row = await cursor.fetchone()
        if row is None:
            return False
        return str(row["event_type"]) in {
            RuntimeEventType.REQUEST_COMPLETED.value,
            RuntimeEventType.REQUEST_FAILED.value,
        }

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        traces = await self.list_recent_traces(app_id, session_id=session_id, limit=1)
        return traces[0] if traces else None

    async def list_recent_traces(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self.open()
        params: list[Any] = [app_id]
        session_clause = ""
        if session_id is not None:
            session_clause = "AND session_id = %s"
            params.append(session_id)
        params.append(max(1, int(limit)))
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT
                        trace_id,
                        session_id,
                        MIN(created_at) AS started_at,
                        MAX(created_at) AS latest_event_at,
                        MAX(event_id) AS latest_event_id,
                        COUNT(*) AS event_count
                    FROM runtime_event_log
                    WHERE app_id = %s
                      AND trace_id IS NOT NULL
                      {session_clause}
                    GROUP BY trace_id, session_id
                    ORDER BY MAX(created_at) DESC, MAX(event_id) DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cursor.fetchall()
        return [self._trace_row_to_summary(row) for row in rows]

    async def _init_schema(self) -> None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS runtime_event_log (
                            event_id BIGSERIAL,
                            request_id TEXT,
                            trace_id TEXT,
                            app_id TEXT NOT NULL,
                            agent_id TEXT NOT NULL,
                            session_id TEXT,
                            seq INTEGER,
                            event_type TEXT NOT NULL,
                            loop_index INTEGER,
                            step_key TEXT,
                            dedupe_key TEXT,
                            payload JSONB NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        ADD COLUMN IF NOT EXISTS event_id BIGSERIAL
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        DROP CONSTRAINT IF EXISTS runtime_event_log_pkey
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        ALTER COLUMN request_id DROP NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        ALTER COLUMN seq DROP NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_event_id
                        ON runtime_event_log(event_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_dedupe
                        ON runtime_event_log(request_id, dedupe_key)
                        WHERE request_id IS NOT NULL AND dedupe_key IS NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_request_seq
                        ON runtime_event_log(request_id, seq)
                        WHERE request_id IS NOT NULL AND seq IS NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_request
                        ON runtime_event_log(request_id, seq)
                        WHERE request_id IS NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_app_cursor
                        ON runtime_event_log(app_id, event_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_session_cursor
                        ON runtime_event_log(app_id, session_id, event_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_trace_cursor
                        ON runtime_event_log(app_id, trace_id, event_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_type
                        ON runtime_event_log(event_type)
                        """
                    )

    @staticmethod
    def _dict_to_event(row: dict[str, Any]) -> RuntimeEvent:
        payload = row.get("payload")
        decoded = payload if isinstance(payload, dict) else {}
        request_seq = row.get("request_seq", row.get("seq"))
        return RuntimeEvent(
            event_id=int(row["event_id"]),
            request_id=(
                str(row["request_id"])
                if row.get("request_id") is not None
                else None
            ),
            request_seq=(int(request_seq) if request_seq is not None else None),
            trace_id=row.get("trace_id"),
            app_id=str(row["app_id"]),
            agent_id=str(row["agent_id"]),
            session_id=row.get("session_id"),
            event_type=str(row["event_type"]),
            loop_index=(
                int(row["loop_index"]) if row.get("loop_index") is not None else None
            ),
            step_key=row.get("step_key"),
            dedupe_key=row.get("dedupe_key"),
            payload=decoded,
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _trace_row_to_summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "trace_id": str(row["trace_id"]),
            "session_id": str(row["session_id"]) if row.get("session_id") else None,
            "event_count": int(row["event_count"]),
            "started_at": float(row["started_at"]),
            "latest_event_at": float(row["latest_event_at"]),
            "latest_event_id": int(row["latest_event_id"]),
        }

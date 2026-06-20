"""Postgres-backed canonical runtime event store."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Protocol, cast

from .types import FeedbackRecord, RuntimeEvent, RuntimeEventType

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
        host_id: str | None = None,
        workflow_run_id: str | None = None,
        event_type_prefix: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        ...

    async def has_request(self, request_id: str) -> bool:
        ...

    async def is_request_terminal(self, request_id: str) -> bool:
        ...

    async def append_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        ...

    async def list_feedback(
        self,
        app_id: str,
        *,
        after: float,
        before: float | None = None,
        feedback_type: str | None = None,
        session_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackRecord]:
        ...

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        ...

    async def list_recent_traces(
        self,
        app_id: str | None = None,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        ...

    async def list_sessions(
        self,
        *,
        owner_agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        ...

    async def aggregate_usage(
        self,
        app_id: str,
        *,
        host_id: str | None = None,
        session_id: str | None = None,
        bucket: str = "day",
        from_ts: float | None = None,
        to_ts: float | None = None,
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
                                   agent_id, session_id, host_id, workflow_id, workflow_run_id,
                                   event_type, loop_index, step_key,
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
                            request_id, trace_id, app_id, agent_id, session_id, host_id,
                            workflow_id, workflow_run_id, seq,
                            event_type, loop_index, step_key, dedupe_key, payload, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        RETURNING event_id, request_id, seq AS request_seq, trace_id, app_id,
                                  agent_id, session_id, host_id, workflow_id, workflow_run_id,
                                  event_type, loop_index, step_key,
                                  dedupe_key, payload, created_at
                        """,
                        (
                            event.request_id,
                            event.trace_id,
                            event.app_id,
                            event.agent_id,
                            event.session_id,
                            event.host_id,
                            event.workflow_id,
                            event.workflow_run_id,
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
                           agent_id, session_id, host_id, event_type, loop_index, step_key,
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
        host_id: str | None = None,
        workflow_run_id: str | None = None,
        event_type_prefix: str | None = None,
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
        if host_id is not None:
            clauses.append("host_id = %s")
            params.append(host_id)
        if workflow_run_id is not None:
            clauses.append("workflow_run_id = %s")
            params.append(workflow_run_id)
        if event_type_prefix is not None:
            clauses.append("event_type LIKE %s")
            params.append(f"{event_type_prefix}%")
        if limit is not None:
            query = f"""
                SELECT event_id, request_id, request_seq, trace_id, app_id,
                       agent_id, session_id, host_id, event_type, loop_index, step_key,
                       dedupe_key, payload, created_at
                FROM (
                    SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                           agent_id, session_id, host_id, event_type, loop_index, step_key,
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
                       agent_id, session_id, host_id, event_type, loop_index, step_key,
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

    async def append_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO runtime_feedback (
                        feedback_type, message, app_id, host_id, session_id,
                        request_id, trace_id, context, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING feedback_id, feedback_type, message, app_id, host_id,
                              session_id, request_id, trace_id, context, created_at
                    """,
                    (
                        feedback.feedback_type,
                        feedback.message,
                        feedback.app_id,
                        feedback.host_id,
                        feedback.session_id,
                        feedback.request_id,
                        feedback.trace_id,
                        json.dumps(feedback.context or {}, ensure_ascii=True, default=str),
                        float(feedback.created_at),
                    ),
                )
                stored = await cursor.fetchone()
        if stored is None:
            raise RuntimeError("failed to persist feedback")
        return self._dict_to_feedback(stored)

    async def list_feedback(
        self,
        app_id: str,
        *,
        after: float,
        before: float | None = None,
        feedback_type: str | None = None,
        session_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackRecord]:
        await self.open()
        clauses = ["app_id = %s", "created_at > %s"]
        params: list[Any] = [app_id, float(after)]
        if before is not None:
            clauses.append("created_at < %s")
            params.append(float(before))
        if feedback_type is not None:
            clauses.append("feedback_type = %s")
            params.append(feedback_type)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)

        query_term = (q or "").strip()
        if query_term:
            clauses.append(
                "to_tsvector('simple', COALESCE(message, '')) "
                "@@ plainto_tsquery('simple', %s)"
            )
            params.append(query_term)
            order_by = (
                "ts_rank_cd(to_tsvector('simple', COALESCE(message, '')), "
                "plainto_tsquery('simple', %s)) DESC, created_at DESC, feedback_id DESC"
            )
            params.append(query_term)
        else:
            order_by = "created_at DESC, feedback_id DESC"

        query = f"""
            SELECT feedback_id, feedback_type, message, app_id, host_id,
                   session_id, request_id, trace_id, context, created_at
            FROM runtime_feedback
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_by}
        """
        if limit is not None:
            query += "\nLIMIT %s"
            params.append(max(1, int(limit)))

        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [self._dict_to_feedback(row) for row in rows]

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        traces = await self.list_recent_traces(app_id, session_id=session_id, limit=1)
        return traces[0] if traces else None

    async def list_recent_traces(
        self,
        app_id: str | None = None,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self.open()
        # app_id=None lists a session's traces across every executing agent
        # (primary + subagents + cross-agent workflow tasks).
        filters = ["trace_id IS NOT NULL"]
        params: list[Any] = []
        if app_id is not None:
            filters.append("app_id = %s")
            params.append(app_id)
        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)
        if host_id is not None:
            filters.append("host_id = %s")
            params.append(host_id)
        params.append(max(1, int(limit)))
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT
                        trace_id,
                        session_id,
                        MAX(host_id) AS host_id,
                        MAX(agent_id) AS agent_id,
                        MAX(workflow_id) AS workflow_id,
                        MAX(workflow_run_id) AS workflow_run_id,
                        MIN(created_at) AS started_at,
                        MAX(created_at) AS latest_event_at,
                        MAX(event_id) AS latest_event_id,
                        COUNT(*) AS event_count
                    FROM runtime_event_log
                    WHERE {' AND '.join(filters)}
                    GROUP BY trace_id, session_id
                    ORDER BY MAX(created_at) DESC, MAX(event_id) DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cursor.fetchall()
        return [self._trace_row_to_summary(row) for row in rows]

    async def list_sessions(
        self,
        *,
        owner_agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Sessions rolled up from the event log, newest activity first.

        A session is the unit that contains traces; its owner is the agent of its
        earliest event (the REPL primary for a chat session, the task agent for a
        fresh API-triggered workflow run). Traces in a session may run on other
        agents (subagents, cross-agent workflow tasks).
        """
        await self.open()
        params: list[Any] = [owner_agent_id, owner_agent_id]
        sql = """
            SELECT * FROM (
                SELECT
                    session_id,
                    (ARRAY_AGG(agent_id ORDER BY created_at ASC, event_id ASC))[1]
                        AS owner_agent_id,
                    MAX(host_id) AS host_id,
                    MIN(created_at) AS started_at,
                    MAX(created_at) AS latest_event_at,
                    COUNT(DISTINCT trace_id) AS trace_count,
                    COALESCE(SUM(
                        COALESCE(
                            NULLIF(payload -> 'token_usage' ->> 'input', '')::numeric,
                            NULLIF(payload -> 'token_usage' ->> 'input_tokens', '')::numeric,
                            NULLIF(payload ->> 'input_tokens', '')::numeric,
                            0
                        )
                        + COALESCE(
                            NULLIF(payload -> 'token_usage' ->> 'output', '')::numeric,
                            NULLIF(payload -> 'token_usage' ->> 'output_tokens', '')::numeric,
                            NULLIF(payload ->> 'output_tokens', '')::numeric,
                            0
                        )
                    ), 0) AS total_tokens
                FROM runtime_event_log
                WHERE session_id IS NOT NULL
                GROUP BY session_id
            ) sessions
            WHERE (%s IS NULL OR sessions.owner_agent_id = %s)
            ORDER BY sessions.latest_event_at DESC, sessions.session_id ASC
        """
        if limit is not None:
            sql += "\nLIMIT %s"
            params.append(max(1, int(limit)))
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, tuple(params))
                rows = await cursor.fetchall()
        return [
            {
                "session_id": str(row["session_id"]),
                "owner_agent_id": (
                    str(row["owner_agent_id"]) if row.get("owner_agent_id") else None
                ),
                "host_id": str(row["host_id"]) if row.get("host_id") else None,
                "started_at": float(row["started_at"]),
                "latest_event_at": float(row["latest_event_at"]),
                "trace_count": int(row["trace_count"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in rows
        ]

    async def aggregate_usage(
        self,
        app_id: str,
        *,
        host_id: str | None = None,
        session_id: str | None = None,
        bucket: str = "day",
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        bucket_seconds = 3600 if str(bucket).lower() == "hour" else 86400
        tool_completed = (
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        )
        # Placeholders are bound in SQL text order: the two bucket divisors and
        # the two tool-completed event types live in the SELECT, ahead of the
        # WHERE-clause filters.
        filters = ["app_id = %s"]
        params: list[Any] = [
            bucket_seconds,
            bucket_seconds,
            tool_completed[0],
            tool_completed[1],
            app_id,
        ]
        if host_id is not None:
            filters.append("host_id = %s")
            params.append(host_id)
        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)
        if from_ts is not None:
            filters.append("created_at >= %s")
            params.append(float(from_ts))
        if to_ts is not None:
            filters.append("created_at < %s")
            params.append(float(to_ts))
        query = f"""
            SELECT
                floor(created_at / %s) * %s AS bucket_start,
                COUNT(DISTINCT trace_id) AS request_count,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(payload -> 'token_usage' ->> 'input', '')::numeric,
                        NULLIF(payload -> 'token_usage' ->> 'input_tokens', '')::numeric,
                        NULLIF(payload ->> 'input_tokens', '')::numeric,
                        0
                    )
                ), 0) AS input_tokens,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(payload -> 'token_usage' ->> 'output', '')::numeric,
                        NULLIF(payload -> 'token_usage' ->> 'output_tokens', '')::numeric,
                        NULLIF(payload ->> 'output_tokens', '')::numeric,
                        0
                    )
                ), 0) AS output_tokens,
                COALESCE(SUM(
                    CASE
                        WHEN event_type IN (%s, %s)
                         AND (payload -> 'result' ->> 'is_error') = 'true'
                        THEN 1 ELSE 0
                    END
                ), 0) AS tool_error_count
            FROM runtime_event_log
            WHERE {' AND '.join(filters)}
            GROUP BY bucket_start
            ORDER BY bucket_start ASC
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [self._usage_row_to_bucket(row) for row in rows]

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
                            host_id TEXT,
                            workflow_id TEXT,
                            workflow_run_id TEXT,
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
                        ADD COLUMN IF NOT EXISTS host_id TEXT
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        ADD COLUMN IF NOT EXISTS workflow_id TEXT
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE runtime_event_log
                        ADD COLUMN IF NOT EXISTS workflow_run_id TEXT
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
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_event_host_cursor
                        ON runtime_event_log(app_id, host_id, event_id)
                        WHERE host_id IS NOT NULL
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS runtime_feedback (
                            feedback_id BIGSERIAL PRIMARY KEY,
                            feedback_type TEXT NOT NULL,
                            message TEXT NOT NULL,
                            app_id TEXT NOT NULL,
                            host_id TEXT,
                            session_id TEXT,
                            request_id TEXT,
                            trace_id TEXT,
                            context JSONB NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_feedback_app_created
                        ON runtime_feedback(app_id, created_at)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_runtime_feedback_message_fts
                        ON runtime_feedback
                        USING GIN (to_tsvector('simple', COALESCE(message, '')))
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
            host_id=row.get("host_id"),
            workflow_id=row.get("workflow_id"),
            workflow_run_id=row.get("workflow_run_id"),
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
    def _dict_to_feedback(row: dict[str, Any]) -> FeedbackRecord:
        context = row.get("context")
        decoded = context if isinstance(context, dict) else {}
        return FeedbackRecord(
            feedback_id=int(row["feedback_id"]),
            feedback_type=str(row["feedback_type"]),
            message=str(row["message"]),
            app_id=str(row["app_id"]),
            host_id=row.get("host_id"),
            session_id=row.get("session_id"),
            request_id=row.get("request_id"),
            trace_id=row.get("trace_id"),
            context=decoded,
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _trace_row_to_summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "trace_id": str(row["trace_id"]),
            "session_id": str(row["session_id"]) if row.get("session_id") else None,
            "host_id": str(row["host_id"]) if row.get("host_id") else None,
            "agent_id": str(row["agent_id"]) if row.get("agent_id") else None,
            "workflow_id": str(row["workflow_id"]) if row.get("workflow_id") else None,
            "workflow_run_id": (
                str(row["workflow_run_id"]) if row.get("workflow_run_id") else None
            ),
            "event_count": int(row["event_count"]),
            "started_at": float(row["started_at"]),
            "latest_event_at": float(row["latest_event_at"]),
            "latest_event_id": int(row["latest_event_id"]),
        }

    @staticmethod
    def _usage_row_to_bucket(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "bucket_start": float(row["bucket_start"]),
            "request_count": int(row["request_count"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "tool_error_count": int(row["tool_error_count"]),
        }

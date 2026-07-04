"""PostgresRuntimeStore — connection pool, pub/sub lifecycle, and protocol delegation."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, cast

from ...protocol import RuntimeStore
from ...types import FeedbackRecord, RuntimeEvent
from . import loaders
from .migrations import run_migrations

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]


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
            await run_migrations(pool)
            self._pool = pool

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

    def unregister_request_waiter(self, request_id: str, event: asyncio.Event) -> None:
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
        return await loaders.append_event(self._pool, event)

    async def list_request_events(
        self, request_id: str, *, after_seq: int = 0
    ) -> list[RuntimeEvent]:
        await self.open()
        return await loaders.list_request_events(
            self._pool, request_id, after_seq=after_seq
        )

    async def list_session_events(
        self,
        session_id: str,
        *,
        event_types: list[str] | None = None,
    ) -> list[RuntimeEvent]:
        await self.open()
        return await loaders.list_session_events(
            self._pool, session_id, event_types=event_types
        )

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
        return await loaders.list_events(
            self._pool,
            app_id,
            session_id=session_id,
            trace_id=trace_id,
            host_id=host_id,
            workflow_run_id=workflow_run_id,
            event_type_prefix=event_type_prefix,
            after_event_id=after_event_id,
            limit=limit,
        )

    async def has_request(self, request_id: str) -> bool:
        await self.open()
        return await loaders.has_request(self._pool, request_id)

    async def is_request_terminal(self, request_id: str) -> bool:
        await self.open()
        return await loaders.is_request_terminal(self._pool, request_id)

    async def append_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        await self.open()
        return await loaders.append_feedback(self._pool, feedback)

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
        return await loaders.list_feedback(
            self._pool,
            app_id,
            after=after,
            before=before,
            feedback_type=feedback_type,
            session_id=session_id,
            q=q,
            limit=limit,
        )

    async def get_latest_trace(
        self, app_id: str, session_id: str
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
        return await loaders.list_recent_traces(
            self._pool,
            app_id,
            session_id=session_id,
            host_id=host_id,
            limit=limit,
        )

    async def list_sessions(
        self,
        *,
        owner_agent_id: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.list_sessions(
            self._pool, owner_agent_id=owner_agent_id, limit=limit
        )

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
        return await loaders.aggregate_usage(
            self._pool,
            app_id,
            host_id=host_id,
            session_id=session_id,
            bucket=bucket,
            from_ts=from_ts,
            to_ts=to_ts,
        )

    async def count_tool_invocations(
        self,
        app_id: str,
        *,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.count_tool_invocations(
            self._pool, app_id, from_ts=from_ts, to_ts=to_ts
        )

    async def count_skill_invocations(
        self,
        app_id: str,
        *,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.count_skill_invocations(
            self._pool, app_id, from_ts=from_ts, to_ts=to_ts
        )

"""PostgresRuntimeStore: the runtime event log over the process's shared pool."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from mash.storage.pool import acquire_pool, release_pool

from ...protocol import RuntimeStore
from ...types import FeedbackRecord, RequestIdentity, RequestStatus, RuntimeEvent
from . import loaders


class PostgresRuntimeStore(RuntimeStore):
    """Append-only Postgres-backed runtime event store.

    A request's events are written and streamed by the same agent process, so
    an append wakes the request's stream waiters in memory.
    """

    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._request_waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    async def open(self) -> None:
        if self._pool is not None:
            return
        async with self._open_lock:
            if self._pool is None:
                self._pool = await acquire_pool(self._database_url)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool = None
            await release_pool(self._database_url)

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

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        await self.open()
        stored = await loaders.append_event(self._pool, event)
        if stored.request_id:
            for waiter in self._request_waiters.get(stored.request_id, ()):
                waiter.set()
        return stored

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

    async def get_request_lifecycle(self, request_id: str) -> RequestStatus | None:
        await self.open()
        return await loaders.get_request_status(self._pool, request_id)

    async def get_request_identity(self, request_id: str) -> RequestIdentity | None:
        await self.open()
        return await loaders.get_request_identity(self._pool, request_id)

    async def save_checkpoint(self, request_id: str, checkpoint: dict[str, Any]) -> None:
        await self.open()
        await loaders.save_checkpoint(self._pool, request_id, checkpoint)

    async def get_checkpoint(self, request_id: str) -> dict[str, Any] | None:
        await self.open()
        return await loaders.get_checkpoint(self._pool, request_id)

    async def list_running_requests(self, agent_id: str) -> list[str]:
        await self.open()
        return await loaders.list_running_requests(self._pool, agent_id)

    async def count_recovery(self, request_id: str) -> int:
        await self.open()
        return await loaders.count_recovery(self._pool, request_id)

    async def read_request_stream(
        self, request_id: str, *, after_seq: int = 0
    ) -> tuple[list[RuntimeEvent], bool]:
        await self.open()
        return await loaders.read_request_stream(
            self._pool, request_id, after_seq=after_seq
        )

    async def get_request_id_for_trace(
        self,
        trace_id: str,
        *,
        app_id: str | None = None,
    ) -> str | None:
        await self.open()
        return await loaders.get_request_id_for_trace(
            self._pool, trace_id, app_id=app_id
        )

    async def list_request_ends(
        self, *, after_event_id: int = 0, limit: int = 500
    ) -> list[RuntimeEvent]:
        await self.open()
        return await loaders.list_request_ends(
            self._pool, after_event_id=after_event_id, limit=limit
        )

    async def get_export_cursor(self, name: str) -> int | None:
        await self.open()
        return await loaders.get_export_cursor(self._pool, name)

    async def latest_event_id(self) -> int:
        await self.open()
        return await loaders.latest_event_id(self._pool)

    async def set_export_cursor(self, name: str, event_id: int) -> None:
        await self.open()
        await loaders.set_export_cursor(self._pool, name, event_id)

    async def list_session_requests(
        self,
        session_id: str,
        *,
        mode: str | None = None,
        status: str | None = None,
        started_by: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.list_session_requests(
            self._pool, session_id, mode=mode, status=status, started_by=started_by
        )

    async def list_child_requests(
        self,
        parent_request_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.list_child_requests(
            self._pool, parent_request_id, status=status
        )

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
        status: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.list_recent_traces(
            self._pool,
            app_id,
            session_id=session_id,
            host_id=host_id,
            status=status,
            limit=limit,
        )

    async def list_thought_traces(
        self, app_id: str | None = None, *, limit: int = 50, trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.open()
        return await loaders.list_thought_traces(self._pool, app_id, limit=limit, trace_id=trace_id)

    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        await self.open()
        return await loaders.list_sessions(
            self._pool, agent_id=agent_id, workflow_id=workflow_id, limit=limit
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

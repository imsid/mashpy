"""Runtime event store protocol and SQLite backend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional, Protocol, Union

import aiosqlite

from .types import RuntimeEvent


class RuntimeStore(Protocol):
    """Append-only durable event store for runtime execution."""

    async def open(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        ...

    async def list_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]:
        ...

    async def has_request(self, request_id: str) -> bool:
        ...

    async def list_incomplete_request_ids(
        self,
        *,
        app_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> list[str]:
        ...


class SQLiteRuntimeStore(RuntimeStore):
    """SQLite-backed append-only runtime event store."""

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._db_path = self._prepare_path(path)
        self._conn: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

    @staticmethod
    def _prepare_path(path: Union[str, Path]) -> str:
        if isinstance(path, Path):
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            return str(path)
        text = str(path)
        if text != ":memory:":
            Path(text).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        return text

    async def open(self) -> None:
        if self._conn is not None:
            return
        async with self._open_lock:
            if self._conn is not None:
                return
            conn = await aiosqlite.connect(self._db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout = 5000")
            if self._db_path != ":memory:":
                try:
                    await conn.execute("PRAGMA journal_mode = WAL")
                except aiosqlite.Error:
                    pass
            self._conn = conn
            await self._init_schema()

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteRuntimeStore is not open")
        return self._conn

    async def _init_schema(self) -> None:
        conn = await self._get_conn()
        async with self._lock:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_event_log (
                    request_id TEXT NOT NULL,
                    trace_id TEXT,
                    app_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    loop_index INTEGER,
                    step_key TEXT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (request_id, seq)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_event_request ON runtime_event_log(request_id, seq)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_event_app_agent ON runtime_event_log(app_id, agent_id, request_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_event_type ON runtime_event_log(event_type)"
            )
            await conn.commit()

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        await self.open()
        conn = await self._get_conn()
        async with self._lock:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM runtime_event_log WHERE request_id = ?",
                (event.request_id,),
            )
            row = await cursor.fetchone()
            next_seq = int(row["max_seq"]) + 1 if row is not None else 1
            stored = RuntimeEvent(
                request_id=event.request_id,
                trace_id=event.trace_id,
                app_id=event.app_id,
                agent_id=event.agent_id,
                session_id=event.session_id,
                seq=next_seq,
                event_type=event.event_type,
                loop_index=event.loop_index,
                step_key=event.step_key,
                payload=dict(event.payload or {}),
                created_at=float(event.created_at),
            )
            await conn.execute(
                """
                INSERT INTO runtime_event_log (
                    request_id, trace_id, app_id, agent_id, session_id, seq,
                    event_type, loop_index, step_key, payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.request_id,
                    stored.trace_id,
                    stored.app_id,
                    stored.agent_id,
                    stored.session_id,
                    stored.seq,
                    stored.event_type,
                    stored.loop_index,
                    stored.step_key,
                    json.dumps(stored.payload, ensure_ascii=True, default=str),
                    stored.created_at,
                ),
            )
            await conn.commit()
        return stored

    async def list_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]:
        await self.open()
        conn = await self._get_conn()
        async with self._lock:
            cursor = await conn.execute(
                """
                SELECT request_id, trace_id, app_id, agent_id, session_id, seq,
                       event_type, loop_index, step_key, payload, created_at
                FROM runtime_event_log
                WHERE request_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (request_id, int(after_seq)),
            )
            rows = await cursor.fetchall()
        return [self._row_to_event(row) for row in rows]

    async def has_request(self, request_id: str) -> bool:
        await self.open()
        conn = await self._get_conn()
        async with self._lock:
            cursor = await conn.execute(
                "SELECT 1 FROM runtime_event_log WHERE request_id = ? LIMIT 1",
                (request_id,),
            )
            row = await cursor.fetchone()
        return row is not None

    async def list_incomplete_request_ids(
        self,
        *,
        app_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> list[str]:
        await self.open()
        conn = await self._get_conn()
        clauses = []
        params: list[Any] = []
        if app_id:
            clauses.append("app_id = ?")
            params.append(app_id)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._lock:
            cursor = await conn.execute(
                f"""
                SELECT request_id
                FROM runtime_event_log
                {where_sql}
                GROUP BY request_id
                HAVING MAX(CASE WHEN event_type IN ('runtime.request.completed', 'runtime.request.failed') THEN 1 ELSE 0 END) = 0
                ORDER BY MIN(created_at) ASC
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
        return [str(row["request_id"]) for row in rows]

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> RuntimeEvent:
        payload = row["payload"]
        decoded = json.loads(payload) if isinstance(payload, str) and payload else {}
        return RuntimeEvent(
            request_id=str(row["request_id"]),
            trace_id=row["trace_id"],
            app_id=str(row["app_id"]),
            agent_id=str(row["agent_id"]),
            session_id=row["session_id"],
            seq=int(row["seq"]),
            event_type=str(row["event_type"]),
            loop_index=(int(row["loop_index"]) if row["loop_index"] is not None else None),
            step_key=row["step_key"],
            payload=decoded if isinstance(decoded, dict) else {},
            created_at=float(row["created_at"]),
        )

"""Postgres-backed store for v2 workflow runs, steps, and the step audit log.

The connection-taking functions (``insert_run``, ``upsert_step``,
``append_step_event`` …) are the atomic write path: Phase 2 calls them with the
DBOS step's own connection so a step's records commit together with its memoized
output. ``WorkflowStore`` wraps them with a pool for standalone use (reads, and
run bookkeeping outside a DBOS step).

Timestamps are epoch seconds, matching the rest of the Mash schema.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from mash.storage.migrations import run_migrations

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # pragma: no cover - dependency missing
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]


# Run statuses (workflow-level).
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_TERMINAL = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})

# Step statuses.
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"

# Step audit event types.
STEP_EVENT_STARTED = "step.started"
STEP_EVENT_COMPLETED = "step.completed"
STEP_EVENT_FAILED = "step.failed"
STEP_EVENT_RETRIED = "step.retried"


@dataclass
class WorkflowRunRecord:
    run_id: str
    workflow_id: str
    status: str
    workflow_input: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    dedup_key: str | None = None
    session_id: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class WorkflowStepRecord:
    run_id: str
    workflow_id: str
    step_id: str
    ordinal: int
    kind: str
    status: str
    input_snapshot: dict[str, Any] | None = None
    output_snapshot: dict[str, Any] | None = None
    error: str | None = None
    attempt: int = 1
    agent_request_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class WorkflowStepEventRecord:
    run_id: str
    workflow_id: str
    step_id: str
    attempt: int
    event_type: str
    seq: int
    at: float
    payload: dict[str, Any] = field(default_factory=dict)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, default=str)


# --- Connection-taking write functions (the atomic path) ---------------------


async def insert_run(conn: Any, run: WorkflowRunRecord) -> None:
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            INSERT INTO workflow_runs (
                run_id, workflow_id, status, workflow_input, result, error,
                dedup_key, session_id, created_at, started_at, finished_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                run.run_id,
                run.workflow_id,
                run.status,
                _json(run.workflow_input or {}),
                _json(run.result),
                run.error,
                run.dedup_key,
                run.session_id,
                run.created_at,
                run.started_at,
                run.finished_at,
            ),
        )


async def mark_run_started(conn: Any, run_id: str, started_at: float) -> None:
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            UPDATE workflow_runs
            SET status = %s, started_at = COALESCE(started_at, %s)
            WHERE run_id = %s
            """,
            (RUN_RUNNING, started_at, run_id),
        )


async def finish_run(
    conn: Any,
    run_id: str,
    *,
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
    finished_at: float,
) -> None:
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            UPDATE workflow_runs
            SET status = %s, result = %s, error = %s, finished_at = %s
            WHERE run_id = %s
            """,
            (status, _json(result), error, finished_at, run_id),
        )


async def upsert_step(conn: Any, step: WorkflowStepRecord) -> None:
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            INSERT INTO workflow_steps (
                run_id, workflow_id, step_id, ordinal, kind, status,
                input_snapshot, output_snapshot, error, attempt,
                agent_request_id, started_at, finished_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, step_id) DO UPDATE SET
                status = EXCLUDED.status,
                input_snapshot = EXCLUDED.input_snapshot,
                output_snapshot = EXCLUDED.output_snapshot,
                error = EXCLUDED.error,
                attempt = EXCLUDED.attempt,
                agent_request_id = EXCLUDED.agent_request_id,
                started_at = COALESCE(workflow_steps.started_at, EXCLUDED.started_at),
                finished_at = EXCLUDED.finished_at
            """,
            (
                step.run_id,
                step.workflow_id,
                step.step_id,
                step.ordinal,
                step.kind,
                step.status,
                _json(step.input_snapshot),
                _json(step.output_snapshot),
                step.error,
                step.attempt,
                step.agent_request_id,
                step.started_at,
                step.finished_at,
            ),
        )


async def append_step_event(
    conn: Any,
    *,
    run_id: str,
    workflow_id: str,
    step_id: str,
    event_type: str,
    at: float,
    attempt: int = 1,
    payload: dict[str, Any] | None = None,
) -> int:
    """Append one step lifecycle event, assigning the next per-step seq.

    Idempotent on ``(run_id, step_id, attempt, event_type)``: a DBOS re-run of
    the step re-appends the same transition and ON CONFLICT DO NOTHING makes it a
    no-op. Returns the seq of the (existing or newly inserted) event. Must run
    inside a transaction; the advisory lock serializes concurrent appends for the
    same step so seq stays gap-free.
    """
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{run_id}:{step_id}",),
        )
        await cursor.execute(
            """
            SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
            FROM workflow_step_events
            WHERE run_id = %s AND step_id = %s
            """,
            (run_id, step_id),
        )
        row = await cursor.fetchone()
        seq = int(row["next_seq"]) if row is not None else 1
        await cursor.execute(
            """
            INSERT INTO workflow_step_events (
                run_id, workflow_id, step_id, attempt, event_type, seq, at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, step_id, attempt, event_type) DO NOTHING
            """,
            (run_id, workflow_id, step_id, attempt, event_type, seq, at, _json(payload or {})),
        )
        await cursor.execute(
            """
            SELECT seq FROM workflow_step_events
            WHERE run_id = %s AND step_id = %s AND attempt = %s AND event_type = %s
            """,
            (run_id, step_id, attempt, event_type),
        )
        existing = await cursor.fetchone()
        return int(existing["seq"]) if existing is not None else seq


# --- Row mapping -------------------------------------------------------------


def _run_from_row(row: dict[str, Any]) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        status=row["status"],
        workflow_input=row.get("workflow_input") or {},
        result=row.get("result"),
        error=row.get("error"),
        dedup_key=row.get("dedup_key"),
        session_id=row.get("session_id"),
        created_at=float(row.get("created_at") or 0.0),
        started_at=_opt_float(row.get("started_at")),
        finished_at=_opt_float(row.get("finished_at")),
    )


def _step_from_row(row: dict[str, Any]) -> WorkflowStepRecord:
    return WorkflowStepRecord(
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        step_id=row["step_id"],
        ordinal=int(row["ordinal"]),
        kind=row["kind"],
        status=row["status"],
        input_snapshot=row.get("input_snapshot"),
        output_snapshot=row.get("output_snapshot"),
        error=row.get("error"),
        attempt=int(row.get("attempt") or 1),
        agent_request_id=row.get("agent_request_id"),
        started_at=_opt_float(row.get("started_at")),
        finished_at=_opt_float(row.get("finished_at")),
    )


def _event_from_row(row: dict[str, Any]) -> WorkflowStepEventRecord:
    return WorkflowStepEventRecord(
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        step_id=row["step_id"],
        attempt=int(row.get("attempt") or 1),
        event_type=row["event_type"],
        seq=int(row["seq"]),
        at=float(row["at"]),
        payload=row.get("payload") or {},
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


# --- Store wrapper -----------------------------------------------------------


class WorkflowStore:
    """Pool-owning Postgres store for workflow runs, steps, and step events."""

    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._pool is not None:
            return
        if dict_row is None or AsyncConnectionPool is None:
            raise RuntimeError(
                "psycopg and psycopg_pool are required for WorkflowStore. "
                "Install mashpy with PostgreSQL runtime dependencies."
            )
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

    async def create_run(self, run: WorkflowRunRecord) -> None:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await insert_run(conn, run)

    async def mark_run_started(self, run_id: str, started_at: float) -> None:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await mark_run_started(conn, run_id, started_at)

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: float,
    ) -> None:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await finish_run(
                    conn,
                    run_id,
                    status=status,
                    result=result,
                    error=error,
                    finished_at=finished_at,
                )

    async def upsert_step(self, step: WorkflowStepRecord) -> None:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await upsert_step(conn, step)

    async def append_step_event(
        self,
        *,
        run_id: str,
        workflow_id: str,
        step_id: str,
        event_type: str,
        at: float,
        attempt: int = 1,
        payload: dict[str, Any] | None = None,
    ) -> int:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                return await append_step_event(
                    conn,
                    run_id=run_id,
                    workflow_id=workflow_id,
                    step_id=step_id,
                    event_type=event_type,
                    at=at,
                    attempt=attempt,
                    payload=payload,
                )

    async def get_run(self, run_id: str) -> WorkflowRunRecord | None:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM workflow_runs WHERE run_id = %s", (run_id,)
                )
                row = await cursor.fetchone()
        return _run_from_row(row) if row is not None else None

    async def list_runs(
        self,
        workflow_id: str,
        *,
        status: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> list[WorkflowRunRecord]:
        await self.open()
        clauses = ["workflow_id = %s"]
        params: list[Any] = [workflow_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if start_time is not None:
            clauses.append("created_at >= %s")
            params.append(start_time)
        if end_time is not None:
            clauses.append("created_at <= %s")
            params.append(end_time)
        order = "DESC" if sort_desc else "ASC"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        query = (
            "SELECT * FROM workflow_runs WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at {order}, run_id {order} LIMIT %s OFFSET %s"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [_run_from_row(row) for row in rows]

    async def get_run_steps(self, run_id: str) -> list[WorkflowStepRecord]:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM workflow_steps WHERE run_id = %s ORDER BY ordinal",
                    (run_id,),
                )
                rows = await cursor.fetchall()
        return [_step_from_row(row) for row in rows]

    async def list_step_events(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        after_seq: int = 0,
    ) -> list[WorkflowStepEventRecord]:
        await self.open()
        clauses = ["run_id = %s"]
        params: list[Any] = [run_id]
        if step_id is not None:
            clauses.append("step_id = %s")
            params.append(step_id)
            clauses.append("seq > %s")
            params.append(max(0, int(after_seq)))
        query = (
            "SELECT * FROM workflow_step_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY at, step_id, seq"
        )
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [_event_from_row(row) for row in rows]


__all__ = [
    "RUN_QUEUED",
    "RUN_RUNNING",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_CANCELLED",
    "RUN_TERMINAL",
    "STEP_PENDING",
    "STEP_RUNNING",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_EVENT_STARTED",
    "STEP_EVENT_COMPLETED",
    "STEP_EVENT_FAILED",
    "STEP_EVENT_RETRIED",
    "WorkflowRunRecord",
    "WorkflowStepRecord",
    "WorkflowStepEventRecord",
    "WorkflowStore",
    "insert_run",
    "mark_run_started",
    "finish_run",
    "upsert_step",
    "append_step_event",
]

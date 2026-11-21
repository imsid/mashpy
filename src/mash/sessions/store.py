"""EventStore: the ``sessions`` and ``events`` tables, read and written by the host."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

from mash.policies import PolicyNotFoundError, PolicyStore
from mash.storage.pool import acquire_pool, release_pool

from .models import (
    CANCELLED,
    QUEUED,
    STARTED,
    EventRecord,
    PolicyInUseError,
    SessionMismatchError,
    SessionRecord,
    event_request_id,
)

_TEST_DATABASE_URL = "postgresql://test/runtime"

_EVENT_COLUMNS = """
    e.session_id, e.event_id, e.seq, e.status, e.request_id, e.body, e.response, e.error,
    EXTRACT(EPOCH FROM e.received_at)::double precision AS received_at,
    EXTRACT(EPOCH FROM e.started_at)::double precision AS started_at,
    EXTRACT(EPOCH FROM e.closed_at)::double precision AS closed_at
"""


def _record(row: dict[str, Any]) -> EventRecord:
    return EventRecord(
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        seq=int(row["seq"]),
        status=str(row["status"]),
        body=dict(row["body"] or {}),
        request_id=row.get("request_id"),
        received_at=float(row["received_at"] or 0.0),
        started_at=row.get("started_at"),
        response=row.get("response"),
        error=row.get("error"),
        closed_at=row.get("closed_at"),
    )


def _json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=True, default=str)


class _InMemoryEventStore:
    def __init__(self, policies: PolicyStore) -> None:
        self._policies = policies
        self.sessions: dict[str, SessionRecord] = {}
        self.events: dict[tuple[str, str], EventRecord] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    def _replace(self, record: EventRecord, **changes: Any) -> EventRecord:
        updated = EventRecord(**{**record.__dict__, **changes})
        self.events[(record.session_id, record.event_id)] = updated
        return updated

    async def _policy(self, policy_id: str) -> dict[str, Any] | None:
        record = await self._policies.get(policy_id)
        return record.policy.model_dump() if record is not None else None

    async def insert_event(
        self, session_id: str, event_id: str, *, host_id: str, policy_id: str, body: dict[str, Any]
    ) -> tuple[EventRecord, bool]:
        async with self._lock:
            existing = self.events.get((session_id, event_id))
            if existing is not None:
                return existing, False
            if await self._policy(policy_id) is None:
                raise PolicyNotFoundError(policy_id)
            session = self.sessions.setdefault(
                session_id, SessionRecord(session_id, host_id, policy_id)
            )
            _check_binding(session, host_id, policy_id)
            self._seq += 1
            record = EventRecord(
                session_id=session_id,
                event_id=event_id,
                seq=self._seq,
                status=QUEUED,
                body=dict(body),
                received_at=time.time(),
            )
            self.events[(session_id, event_id)] = record
            return record, True

    async def claim_next(
        self, session_id: str, host_defined: Callable[[str], bool]
    ) -> tuple[EventRecord, SessionRecord, dict[str, Any]] | None:
        async with self._lock:
            session = self.sessions.get(session_id)
            if session is None:
                return None
            rows = [e for e in self.events.values() if e.session_id == session_id]
            if any(e.status == STARTED for e in rows):
                return None
            queued = sorted((e for e in rows if e.status == QUEUED), key=lambda e: e.seq)
            if not queued or not host_defined(session.host_id):
                return None
            policy = await self._policy(session.policy_id)
            if policy is None:
                return None
            claimed = self._replace(
                queued[0],
                status=STARTED,
                request_id=event_request_id(session_id, queued[0].event_id),
                started_at=time.time(),
            )
            return claimed, session, policy

    async def close_event(
        self, session_id: str, event_id: str, *, status: str, response: Any, error: Any
    ) -> bool:
        async with self._lock:
            record = self.events.get((session_id, event_id))
            if record is None or record.status != STARTED:
                return False
            self._replace(
                record, status=status, response=response, error=error, closed_at=time.time()
            )
            return True

    async def cancel_queued(self, session_id: str, event_id: str) -> EventRecord | None:
        async with self._lock:
            record = self.events.get((session_id, event_id))
            if record is None or record.status != QUEUED:
                return None
            return self._replace(record, status=CANCELLED, closed_at=time.time())

    async def get_event(self, session_id: str, event_id: str) -> EventRecord | None:
        return self.events.get((session_id, event_id))

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    async def list_events(
        self,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        statuses: Iterable[str] | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[EventRecord]:
        wanted = set(statuses) if statuses is not None else None
        rows = [
            e
            for e in self.events.values()
            if (session_id is None or e.session_id == session_id)
            and (host_id is None or self.sessions[e.session_id].host_id == host_id)
            and (wanted is None or e.status in wanted)
            and e.seq > after_seq
        ]
        return sorted(rows, key=lambda e: e.seq)[:limit]

    async def sessions_with_queued(self, host_id: str | None = None) -> list[str]:
        return sorted(
            {
                e.session_id
                for e in self.events.values()
                if e.status == QUEUED
                and (host_id is None or self.sessions[e.session_id].host_id == host_id)
            }
        )

    async def delete_policy(
        self, policy_id: str, remove: Callable[[], Awaitable[None]]
    ) -> None:
        async with self._lock:
            if await self._policy(policy_id) is None:
                raise PolicyNotFoundError(policy_id)
            sessions = {s.session_id for s in self.sessions.values() if s.policy_id == policy_id}
            if any(
                e.session_id in sessions and e.status in (QUEUED, STARTED)
                for e in self.events.values()
            ):
                raise PolicyInUseError(policy_id)
            await remove()
            self._policies._mem.records.pop(policy_id, None)  # type: ignore[union-attr]


def _check_binding(session: SessionRecord, host_id: str, policy_id: str) -> None:
    if session.host_id != host_id:
        raise SessionMismatchError(session.session_id, "host", session.host_id)
    if session.policy_id != policy_id:
        raise SessionMismatchError(session.session_id, "policy", session.policy_id)


class EventStore:
    def __init__(self, database_url: str, policies: PolicyStore) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._mem: _InMemoryEventStore | None = (
            _InMemoryEventStore(policies) if resolved == _TEST_DATABASE_URL else None
        )

    async def open(self) -> None:
        if self._mem is not None or self._pool is not None:
            return
        async with self._open_lock:
            if self._pool is None:
                self._pool = await acquire_pool(self._database_url)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool = None
            await release_pool(self._database_url)

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                return list(await cursor.fetchall())

    async def insert_event(
        self, session_id: str, event_id: str, *, host_id: str, policy_id: str, body: dict[str, Any]
    ) -> tuple[EventRecord, bool]:
        """Insert a queued event; ``(record, created)``.

        A seen ``(session_id, event_id)`` returns its row and changes nothing.
        The policy is read ``FOR SHARE``, so a policy delete either sees this
        event or commits first and this insert finds no policy. Raises
        ``PolicyNotFoundError`` and ``SessionMismatchError``.
        """
        if self._mem is not None:
            return await self._mem.insert_event(
                session_id, event_id, host_id=host_id, policy_id=policy_id, body=body
            )
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    existing = await self._get(cursor, session_id, event_id)
                    if existing is not None:
                        return existing, False
                    await cursor.execute(
                        "SELECT 1 FROM policies WHERE policy_id = %s FOR SHARE", (policy_id,)
                    )
                    if await cursor.fetchone() is None:
                        raise PolicyNotFoundError(policy_id)
                    await cursor.execute(
                        """
                        INSERT INTO sessions (session_id, host_id, policy_id)
                        VALUES (%s, %s, %s) ON CONFLICT (session_id) DO NOTHING
                        """,
                        (session_id, host_id, policy_id),
                    )
                    await cursor.execute(
                        "SELECT host_id, policy_id FROM sessions WHERE session_id = %s",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    _check_binding(
                        SessionRecord(session_id, row["host_id"], row["policy_id"]),
                        host_id,
                        policy_id,
                    )
                    await cursor.execute(
                        f"""
                        WITH inserted AS (
                            INSERT INTO events (session_id, event_id, status, body)
                            VALUES (%s, %s, 'queued', %s::jsonb)
                            ON CONFLICT (session_id, event_id) DO NOTHING
                            RETURNING *
                        )
                        SELECT {_EVENT_COLUMNS} FROM inserted AS e
                        """,
                        (session_id, event_id, _json(body)),
                    )
                    inserted = await cursor.fetchone()
                    if inserted is not None:
                        return _record(inserted), True
                    # A concurrent insert of the same event won.
                    existing = await self._get(cursor, session_id, event_id)
                    assert existing is not None
                    return existing, False

    async def claim_next(
        self, session_id: str, host_defined: Callable[[str], bool]
    ) -> tuple[EventRecord, SessionRecord, dict[str, Any]] | None:
        """Start the session's oldest queued event, when it may start.

        It may when the session has no started event and its host is defined
        on this process. Returns the started event, its session and the policy
        it runs under, read in the same transaction.
        """
        if self._mem is not None:
            return await self._mem.claim_next(session_id, host_defined)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT host_id, policy_id FROM sessions WHERE session_id = %s FOR UPDATE",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    session = SessionRecord(session_id, row["host_id"], row["policy_id"])
                    await cursor.execute(
                        "SELECT 1 FROM events WHERE session_id = %s AND status = 'started'",
                        (session_id,),
                    )
                    if await cursor.fetchone() is not None:
                        return None
                    await cursor.execute(
                        """
                        SELECT event_id FROM events
                        WHERE session_id = %s AND status = 'queued'
                        ORDER BY seq LIMIT 1
                        """,
                        (session_id,),
                    )
                    queued = await cursor.fetchone()
                    if queued is None or not host_defined(session.host_id):
                        return None
                    await cursor.execute(
                        "SELECT policy FROM policies WHERE policy_id = %s FOR SHARE",
                        (session.policy_id,),
                    )
                    policy_row = await cursor.fetchone()
                    if policy_row is None:
                        return None
                    event_id = str(queued["event_id"])
                    await cursor.execute(
                        f"""
                        WITH started AS (
                            UPDATE events
                            SET status = 'started', request_id = %s, started_at = now()
                            WHERE session_id = %s AND event_id = %s AND status = 'queued'
                            RETURNING *
                        )
                        SELECT {_EVENT_COLUMNS} FROM started AS e
                        """,
                        (event_request_id(session_id, event_id), session_id, event_id),
                    )
                    started = await cursor.fetchone()
        if started is None:
            return None
        return _record(started), session, dict(policy_row["policy"])

    async def close_event(
        self, session_id: str, event_id: str, *, status: str, response: Any, error: Any
    ) -> bool:
        """Copy a request's outcome onto its started event; False if it was not started."""
        if self._mem is not None:
            return await self._mem.close_event(
                session_id, event_id, status=status, response=response, error=error
            )
        rows = await self._fetch(
            """
            UPDATE events
            SET status = %s, response = %s::jsonb, error = %s::jsonb, closed_at = now()
            WHERE session_id = %s AND event_id = %s AND status = 'started'
            RETURNING event_id
            """,
            (status, _json(response), _json(error), session_id, event_id),
        )
        return bool(rows)

    async def cancel_queued(self, session_id: str, event_id: str) -> EventRecord | None:
        """Cancel a queued event; None when it is not queued."""
        if self._mem is not None:
            return await self._mem.cancel_queued(session_id, event_id)
        rows = await self._fetch(
            f"""
            WITH cancelled AS (
                UPDATE events SET status = 'cancelled', closed_at = now()
                WHERE session_id = %s AND event_id = %s AND status = 'queued'
                RETURNING *
            )
            SELECT {_EVENT_COLUMNS} FROM cancelled AS e
            """,
            (session_id, event_id),
        )
        return _record(rows[0]) if rows else None

    async def _get(self, cursor: Any, session_id: str, event_id: str) -> EventRecord | None:
        await cursor.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events AS e WHERE e.session_id = %s AND e.event_id = %s",
            (session_id, event_id),
        )
        row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def get_event(self, session_id: str, event_id: str) -> EventRecord | None:
        if self._mem is not None:
            return await self._mem.get_event(session_id, event_id)
        rows = await self._fetch(
            f"SELECT {_EVENT_COLUMNS} FROM events AS e WHERE e.session_id = %s AND e.event_id = %s",
            (session_id, event_id),
        )
        return _record(rows[0]) if rows else None

    async def get_session(self, session_id: str) -> SessionRecord | None:
        if self._mem is not None:
            return await self._mem.get_session(session_id)
        rows = await self._fetch(
            "SELECT session_id, host_id, policy_id FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        if not rows:
            return None
        return SessionRecord(rows[0]["session_id"], rows[0]["host_id"], rows[0]["policy_id"])

    async def list_events(
        self,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        statuses: Iterable[str] | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[EventRecord]:
        """Events in seq order, of one session or of every session on a host."""
        if self._mem is not None:
            return await self._mem.list_events(
                session_id=session_id,
                host_id=host_id,
                statuses=statuses,
                after_seq=after_seq,
                limit=limit,
            )
        clauses = ["e.seq > %s"]
        params: list[Any] = [int(after_seq)]
        if session_id is not None:
            clauses.append("e.session_id = %s")
            params.append(session_id)
        if host_id is not None:
            clauses.append("s.host_id = %s")
            params.append(host_id)
        if statuses is not None:
            clauses.append("e.status = ANY(%s)")
            params.append(list(statuses))
        params.append(int(limit))
        rows = await self._fetch(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM events AS e JOIN sessions AS s ON s.session_id = e.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.seq LIMIT %s
            """,
            tuple(params),
        )
        return [_record(row) for row in rows]

    async def sessions_with_queued(self, host_id: str | None = None) -> list[str]:
        """Sessions with a queued event, on ``host_id`` or on any host."""
        if self._mem is not None:
            return await self._mem.sessions_with_queued(host_id)
        rows = await self._fetch(
            """
            SELECT DISTINCT e.session_id
            FROM events AS e JOIN sessions AS s ON s.session_id = e.session_id
            WHERE e.status = 'queued' AND (%s::text IS NULL OR s.host_id = %s)
            ORDER BY e.session_id
            """,
            (host_id, host_id),
        )
        return [str(row["session_id"]) for row in rows]

    async def delete_policy(
        self, policy_id: str, remove: Callable[[], Awaitable[None]]
    ) -> None:
        """Delete a policy that no queued or started event needs.

        The policy row is locked ``FOR UPDATE`` before the check, so an event
        inserted under it either shows here or finds no policy. ``remove``
        runs before the row goes, holding the lock. Raises
        ``PolicyNotFoundError`` and ``PolicyInUseError``.
        """
        if self._mem is not None:
            await self._mem.delete_policy(policy_id, remove)
            return
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT 1 FROM policies WHERE policy_id = %s FOR UPDATE", (policy_id,)
                    )
                    if await cursor.fetchone() is None:
                        raise PolicyNotFoundError(policy_id)
                    await cursor.execute(
                        """
                        SELECT 1 FROM events AS e JOIN sessions AS s ON s.session_id = e.session_id
                        WHERE s.policy_id = %s AND e.status IN ('queued', 'started')
                        LIMIT 1
                        """,
                        (policy_id,),
                    )
                    if await cursor.fetchone() is not None:
                        raise PolicyInUseError(policy_id)
                    await remove()
                    await cursor.execute(
                        "DELETE FROM policies WHERE policy_id = %s", (policy_id,)
                    )

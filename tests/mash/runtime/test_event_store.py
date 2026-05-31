"""Regression tests for the Postgres-backed runtime event store."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from mash.runtime import HostBuilder
from mash.runtime.events.store import PostgresRuntimeStore
from mash.runtime.events.types import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import build_spec

try:  # pragma: no cover - environment dependent
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised when optional deps are missing
    psycopg = None
    dict_row = None


def _runtime_database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash",
    )


def _require_postgres_runtime_support() -> tuple[Any, str]:
    if psycopg is None or dict_row is None:
        raise unittest.SkipTest("psycopg is not installed")
    database_url = _runtime_database_url()
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres runtime store unavailable: {exc}") from exc
    return psycopg, database_url


def _delete_request_rows(database_url: str, *request_ids: str) -> None:
    ids = [value for value in request_ids if value]
    if not ids:
        return
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM runtime_event_log WHERE request_id = ANY(%s)",
                (ids,),
            )


def _delete_app_session_rows(database_url: str, *, app_id: str, session_id: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM runtime_event_log WHERE app_id = %s AND session_id = %s",
                (app_id, session_id),
            )


def _fetch_committed_rows(database_url: str, request_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, request_id, app_id, agent_id, seq, event_type
                FROM runtime_event_log
                WHERE request_id = %s
                ORDER BY seq ASC
                """,
                (request_id,),
            )
            return [dict(row) for row in cursor.fetchall()]


async def _collect_events(
    client: Any,
    request_id: str,
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for event in client.stream_response(request_id, timeout=timeout):
        events.append(event)
        if event.get("event") in {"request.completed", "request.error"}:
            break
    return events


class PostgresRuntimeStoreRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _require_postgres_runtime_support()
        self.database_url = _runtime_database_url()
        self.request_id = f"store-{uuid.uuid4()}"
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()
        _delete_request_rows(self.database_url, self.request_id)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_request_rows(self.database_url, self.request_id)
        _delete_app_session_rows(
            self.database_url,
            app_id="store-test",
            session_id="session-1",
        )

    async def test_reads_do_not_hide_later_committed_writes(self) -> None:
        self.assertFalse(await self.store.has_request(self.request_id))

        accepted = await self.store.append_event(
            RuntimeEvent(
                app_id="store-test",
                agent_id="store-test",
                request_id=self.request_id,
                session_id="session-1",
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key="request.accepted",
                payload={"status": "accepted"},
            )
        )
        self.assertEqual(accepted.request_seq, 1)
        self.assertGreater(int(accepted.event_id), 0)
        self.assertEqual(
            [row["event_type"] for row in _fetch_committed_rows(self.database_url, self.request_id)],
            [RuntimeEventType.REQUEST_ACCEPTED.value],
        )

        listed = await self.store.list_request_events(self.request_id)
        self.assertEqual([event.request_seq for event in listed], [1])
        self.assertFalse(await self.store.is_request_terminal(self.request_id))

        completed = await self.store.append_event(
            RuntimeEvent(
                app_id="store-test",
                agent_id="store-test",
                request_id=self.request_id,
                session_id="session-1",
                event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                dedupe_key="request.completed",
                payload={"status": "completed"},
            )
        )
        self.assertEqual(completed.request_seq, 2)
        self.assertEqual(
            [row["event_type"] for row in _fetch_committed_rows(self.database_url, self.request_id)],
            [
                RuntimeEventType.REQUEST_ACCEPTED.value,
                RuntimeEventType.REQUEST_COMPLETED.value,
            ],
        )
        self.assertTrue(await self.store.is_request_terminal(self.request_id))

    async def test_session_scoped_events_are_queryable_without_request_id(self) -> None:
        event = await self.store.append_event(
            RuntimeEvent(
                app_id="store-test",
                agent_id="store-test",
                session_id="session-1",
                trace_id="trace-1",
                event_type="llm.request.complete",
                payload={"model": "test-model", "duration_ms": 12},
            )
        )
        self.assertIsNone(event.request_id)
        self.assertIsNone(event.request_seq)
        listed = await self.store.list_events(
            "store-test",
            session_id="session-1",
            trace_id="trace-1",
        )
        self.assertEqual([item.event_id for item in listed], [event.event_id])

        latest_trace = await self.store.get_latest_trace("store-test", "session-1")
        self.assertIsNotNone(latest_trace)
        assert latest_trace is not None
        self.assertEqual(latest_trace["trace_id"], "trace-1")


    async def test_request_waiter_wakes_on_append(self) -> None:
        waiter = self.store.register_request_waiter(self.request_id)
        try:
            append_task = asyncio.create_task(self._delayed_append(0.1))
            try:
                await asyncio.wait_for(waiter.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.fail("waiter was not woken by append_event")
            await append_task
        finally:
            self.store.unregister_request_waiter(self.request_id, waiter)

        events = await self.store.list_request_events(self.request_id)
        self.assertEqual(len(events), 1)

    async def test_global_waiter_wakes_on_append(self) -> None:
        waiter = self.store.register_global_waiter()
        try:
            append_task = asyncio.create_task(self._delayed_append(0.1))
            try:
                await asyncio.wait_for(waiter.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.fail("global waiter was not woken by append_event")
            await append_task
        finally:
            self.store.unregister_global_waiter(waiter)

    async def _delayed_append(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.store.append_event(
            RuntimeEvent(
                app_id="store-test",
                agent_id="store-test",
                request_id=self.request_id,
                session_id="session-1",
                event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                dedupe_key=f"waiter-test-{uuid.uuid4().hex[:8]}",
                payload={"status": "accepted"},
            )
        )


class HostedRuntimeEventVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_request_events_are_visible_from_separate_session(self) -> None:
        _require_postgres_runtime_support()
        database_url = _runtime_database_url()
        request_id = ""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "MASH_DATA_DIR": tmp,
                    "MASH_DATABASE_URL": database_url,
                },
            ):
                with patch(
                    "mash.runtime.service.PostgresRuntimeStore",
                    PostgresRuntimeStore,
                ):
                    host = HostBuilder().primary(
                        build_spec(agent_id="primary", response_text="primary-ok")
                    ).build()
                    await host.start()
                    try:
                        client = host.get_client("primary")
                        request_id = await client.post_request("hello", session_id="s-1")
                        events = await _collect_events(client, request_id, timeout=5)
                        self.assertEqual(events[-1]["event"], "request.completed")

                        committed = _fetch_committed_rows(database_url, request_id)
                        self.assertGreaterEqual(len(committed), 2)
                        event_types = [row["event_type"] for row in committed]
                        self.assertIn(RuntimeEventType.REQUEST_ACCEPTED.value, event_types)
                        self.assertIn(RuntimeEventType.REQUEST_COMPLETED.value, event_types)
                    finally:
                        await host.close()
                        _delete_request_rows(database_url, request_id)

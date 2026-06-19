"""Regression tests for the Postgres-backed runtime event store."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from typing import Any

from mash.runtime.events.store import PostgresRuntimeStore
from mash.runtime.events.types import FeedbackRecord, RuntimeEvent, RuntimeEventType

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


def _delete_app_rows(database_url: str, app_id: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM runtime_event_log WHERE app_id = %s",
                (app_id,),
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
    async def asyncSetUp(self) -> None:
        _require_postgres_runtime_support()
        self.database_url = _runtime_database_url()
        self.request_id = f"visible-{uuid.uuid4()}"
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()
        _delete_request_rows(self.database_url, self.request_id)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_request_rows(self.database_url, self.request_id)

    async def test_streamed_request_events_are_visible_from_separate_session(self) -> None:
        # A streaming consumer subscribes through a request waiter. Events
        # appended to the store must both wake that waiter (so a live stream
        # observes them) and be committed durably, so a separate database
        # connection sees the same rows.
        waiter = self.store.register_request_waiter(self.request_id)
        try:
            for dedupe_key, event_type in (
                ("request.accepted", RuntimeEventType.REQUEST_ACCEPTED.value),
                ("request.completed", RuntimeEventType.REQUEST_COMPLETED.value),
            ):
                await self.store.append_event(
                    RuntimeEvent(
                        app_id="store-test",
                        agent_id="store-test",
                        request_id=self.request_id,
                        session_id="session-1",
                        event_type=event_type,
                        dedupe_key=dedupe_key,
                        payload={},
                    )
                )
            self.assertTrue(waiter.is_set())
        finally:
            self.store.unregister_request_waiter(self.request_id, waiter)

        committed = _fetch_committed_rows(self.database_url, self.request_id)
        event_types = [row["event_type"] for row in committed]
        self.assertIn(RuntimeEventType.REQUEST_ACCEPTED.value, event_types)
        self.assertIn(RuntimeEventType.REQUEST_COMPLETED.value, event_types)


def _delete_feedback_rows(database_url: str, app_id: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM runtime_feedback WHERE app_id = %s", (app_id,))


class PostgresFeedbackStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _require_postgres_runtime_support()
        self.database_url = _runtime_database_url()
        self.app_id = f"feedback-{uuid.uuid4()}"
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()
        _delete_feedback_rows(self.database_url, self.app_id)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_feedback_rows(self.database_url, self.app_id)

    async def test_append_and_list_feedback_round_trip(self) -> None:
        stored = await self.store.append_feedback(
            FeedbackRecord(
                app_id=self.app_id,
                message="the trace output is hard to read",
                host_id="assistant",
                session_id="s-1",
                request_id="r-9",
                context={"ts": 1.0},
            )
        )
        self.assertGreater(stored.feedback_id, 0)
        self.assertEqual(stored.feedback_type, "text")

        listed = await self.store.list_feedback(self.app_id, after=0.0, limit=10)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].message, "the trace output is hard to read")
        self.assertEqual(listed[0].session_id, "s-1")
        self.assertEqual(listed[0].context, {"ts": 1.0})

    async def test_list_feedback_filters_and_orders(self) -> None:
        await self.store.append_feedback(
            FeedbackRecord(
                app_id=self.app_id,
                message="streaming hangs on long replies",
                session_id="s-1",
                created_at=100.0,
            )
        )
        await self.store.append_feedback(
            FeedbackRecord(
                app_id=self.app_id,
                message="trace rendering needs colors",
                session_id="s-2",
                created_at=200.0,
            )
        )

        # Default sort is created_at DESC.
        listed = await self.store.list_feedback(self.app_id, after=0.0)
        self.assertEqual([f.created_at for f in listed], [200.0, 100.0])

        # after is an exclusive created_at lower bound.
        after_first = await self.store.list_feedback(self.app_id, after=150.0)
        self.assertEqual([f.session_id for f in after_first], ["s-2"])

        # before is an exclusive upper bound.
        before_second = await self.store.list_feedback(self.app_id, after=0.0, before=150.0)
        self.assertEqual([f.session_id for f in before_second], ["s-1"])

        # session filter.
        only_s1 = await self.store.list_feedback(self.app_id, after=0.0, session_id="s-1")
        self.assertEqual([f.session_id for f in only_s1], ["s-1"])

        # full-text q matches on message terms.
        matched = await self.store.list_feedback(self.app_id, after=0.0, q="streaming")
        self.assertEqual([f.session_id for f in matched], ["s-1"])


class TraceHostFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _require_postgres_runtime_support()
        self.database_url = _runtime_database_url()
        self.app_id = f"traces-{uuid.uuid4().hex[:8]}"
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()
        _delete_app_rows(self.database_url, self.app_id)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_app_rows(self.database_url, self.app_id)

    async def _append(self, *, trace_id: str, host_id: str, created_at: float) -> None:
        await self.store.append_event(
            RuntimeEvent(
                app_id=self.app_id,
                agent_id=self.app_id,
                session_id="session-1",
                trace_id=trace_id,
                host_id=host_id,
                event_type=RuntimeEventType.TRACE_STARTED.value,
                created_at=created_at,
                payload={"message": "hi"},
            )
        )

    async def test_summaries_carry_host_id(self) -> None:
        await self._append(trace_id="t-a", host_id="assistant", created_at=100.0)
        await self._append(trace_id="t-b", host_id="ops", created_at=200.0)

        traces = await self.store.list_recent_traces(self.app_id)
        by_trace = {item["trace_id"]: item for item in traces}
        self.assertEqual(by_trace["t-a"]["host_id"], "assistant")
        self.assertEqual(by_trace["t-b"]["host_id"], "ops")

    async def test_host_id_filter(self) -> None:
        await self._append(trace_id="t-a", host_id="assistant", created_at=100.0)
        await self._append(trace_id="t-b", host_id="ops", created_at=200.0)

        only_ops = await self.store.list_recent_traces(self.app_id, host_id="ops")
        self.assertEqual([item["trace_id"] for item in only_ops], ["t-b"])
        self.assertTrue(all(item["host_id"] == "ops" for item in only_ops))


class AggregateUsageTests(unittest.IsolatedAsyncioTestCase):
    DAY = 86400.0

    async def asyncSetUp(self) -> None:
        _require_postgres_runtime_support()
        self.database_url = _runtime_database_url()
        self.app_id = f"usage-{uuid.uuid4().hex[:8]}"
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()
        _delete_app_rows(self.database_url, self.app_id)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _delete_app_rows(self.database_url, self.app_id)

    async def _append(
        self,
        *,
        trace_id: str,
        event_type: str,
        created_at: float,
        payload: dict[str, Any],
    ) -> None:
        await self.store.append_event(
            RuntimeEvent(
                app_id=self.app_id,
                agent_id=self.app_id,
                session_id="session-1",
                trace_id=trace_id,
                host_id="assistant",
                event_type=event_type,
                created_at=created_at,
                payload=payload,
            )
        )

    async def test_buckets_sum_tokens_requests_and_tool_errors(self) -> None:
        # Two day-buckets, anchored so each timestamp lands mid-day.
        day1 = self.DAY * 19600 + 100.0
        day2 = self.DAY * 19602 + 100.0

        # Day 1: one trace, two LLM steps, one failed tool call.
        await self._append(
            trace_id="t-1",
            event_type=RuntimeEventType.STEP_COMPLETED.value,
            created_at=day1,
            payload={"token_usage": {"input": 100, "output": 10}},
        )
        await self._append(
            trace_id="t-1",
            event_type=RuntimeEventType.STEP_COMPLETED.value,
            created_at=day1 + 1.0,
            payload={"token_usage": {"input": 200, "output": 20}},
        )
        await self._append(
            trace_id="t-1",
            event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
            created_at=day1 + 2.0,
            payload={"tool_name": "x", "result": {"is_error": True}},
        )

        # Day 2: two distinct traces, one LLM step each.
        await self._append(
            trace_id="t-2",
            event_type=RuntimeEventType.STEP_COMPLETED.value,
            created_at=day2,
            payload={"token_usage": {"input": 50, "output": 5}},
        )
        await self._append(
            trace_id="t-3",
            event_type=RuntimeEventType.STEP_COMPLETED.value,
            created_at=day2 + 1.0,
            payload={"token_usage": {"input": 70, "output": 7}},
        )

        buckets = await self.store.aggregate_usage(self.app_id, bucket="day")
        self.assertEqual(len(buckets), 2)

        first, second = buckets
        self.assertEqual(first["bucket_start"], self.DAY * 19600)
        self.assertEqual(first["request_count"], 1)
        self.assertEqual(first["input_tokens"], 300)
        self.assertEqual(first["output_tokens"], 30)
        self.assertEqual(first["tool_error_count"], 1)

        self.assertEqual(second["bucket_start"], self.DAY * 19602)
        self.assertEqual(second["request_count"], 2)
        self.assertEqual(second["input_tokens"], 120)
        self.assertEqual(second["output_tokens"], 12)
        self.assertEqual(second["tool_error_count"], 0)

    async def test_time_window_and_host_filter(self) -> None:
        day1 = self.DAY * 19600 + 100.0
        day2 = self.DAY * 19602 + 100.0
        for trace_id, created_at in (("t-1", day1), ("t-2", day2)):
            await self._append(
                trace_id=trace_id,
                event_type=RuntimeEventType.STEP_COMPLETED.value,
                created_at=created_at,
                payload={"token_usage": {"input": 10, "output": 1}},
            )

        windowed = await self.store.aggregate_usage(
            self.app_id, bucket="day", from_ts=day2 - 50.0
        )
        self.assertEqual([b["bucket_start"] for b in windowed], [self.DAY * 19602])

        wrong_host = await self.store.aggregate_usage(self.app_id, host_id="nope")
        self.assertEqual(wrong_host, [])

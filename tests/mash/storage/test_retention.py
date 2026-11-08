"""Retention deletes only what finished before the window, against real Postgres.

Every seeded row is dated in 1970 and the cutoff is in 1970 too, so running
this against a database with real data deletes none of it.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
import uuid
from typing import Any

import pytest

from mash.storage.pool import acquire_pool, release_pool
from mash.storage.retention import (
    MASH_RETENTION_DAYS_ENV,
    Retention,
    purge,
    retention_days,
)

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

OLD = 1000.0
CUTOFF = 2000.0
RECENT = 3000.0


def _database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )


def test_retention_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASH_RETENTION_DAYS_ENV, raising=False)
    assert retention_days() is None
    assert Retention.from_env("postgresql://x/y") is None


def test_retention_days_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASH_RETENTION_DAYS_ENV, "30")
    assert retention_days() == 30.0
    retention = Retention.from_env("postgresql://x/y")
    assert retention is not None and retention.days == 30.0


@pytest.mark.parametrize("raw", ["0", "-1", "a month"])
def test_bad_retention_days_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(MASH_RETENTION_DAYS_ENV, raw)
    with pytest.raises(ValueError, match=MASH_RETENTION_DAYS_ENV):
        retention_days()


class RetentionPurgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        self.url = _database_url()
        try:
            with psycopg.connect(self.url, autocommit=True) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on local postgres
            raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
        self.pool = await acquire_pool(self.url)
        self.tag = f"ret-{uuid.uuid4().hex[:10]}"
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self) -> None:
        like = f"{self.tag}%"
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DELETE FROM runtime_event_log WHERE app_id LIKE %s", (like,))
            conn.execute("DELETE FROM runtime_request WHERE request_id LIKE %s", (like,))
            conn.execute("DELETE FROM runtime_request_seq WHERE request_id LIKE %s", (like,))
            conn.execute("DELETE FROM runtime_blob WHERE sha256 LIKE %s", (like,))
            conn.execute("DELETE FROM workflow_step_events WHERE run_id LIKE %s", (like,))
            conn.execute("DELETE FROM workflow_runs WHERE run_id LIKE %s", (like,))
            conn.execute("DELETE FROM eval WHERE eval_id LIKE %s", (like,))
        await release_pool(self.url)

    def _sql(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url, autocommit=True) as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def _request(self, name: str, status: str, updated_at: float, refs: list[str] = ()) -> str:
        request_id = f"{self.tag}-{name}"
        self._sql(
            "INSERT INTO runtime_request (request_id, status, updated_at, blob_refs) "
            "VALUES (%s, %s, %s, %s)",
            (request_id, status, updated_at, list(refs)),
        )
        self._sql(
            "INSERT INTO runtime_request_seq (request_id, last_seq) VALUES (%s, 2)",
            (request_id,),
        )
        for seq in (1, 2):
            self._event(request_id, seq, updated_at)
        return request_id

    def _event(self, request_id: str | None, seq: int | None, created_at: float) -> None:
        self._sql(
            "INSERT INTO runtime_event_log "
            "(request_id, app_id, agent_id, seq, event_type, payload, created_at) "
            "VALUES (%s, %s, 'a', %s, 'chat.delta', '{}'::jsonb, %s)",
            (request_id, f"{self.tag}-app", seq, created_at),
        )

    def _run(self, name: str, status: str, finished_at: float | None) -> str:
        run_id = f"{self.tag}-{name}"
        self._sql(
            "INSERT INTO workflow_runs (run_id, workflow_id, status, created_at, finished_at) "
            "VALUES (%s, 'wf', %s, %s, %s)",
            (run_id, status, OLD, finished_at),
        )
        self._sql(
            "INSERT INTO workflow_steps (run_id, workflow_id, step_id, ordinal, kind, status) "
            "VALUES (%s, 'wf', 's1', 0, 'code', 'completed')",
            (run_id,),
        )
        self._sql(
            "INSERT INTO workflow_step_events (run_id, workflow_id, step_id, event_type, seq, at) "
            "VALUES (%s, 'wf', 's1', 'step.completed', 1, %s)",
            (run_id, OLD),
        )
        return run_id

    def _blob(self, name: str, last_used_at: float) -> str:
        sha = f"{self.tag}-{name}"
        self._sql(
            "INSERT INTO runtime_blob (sha256, media_type, data, last_used_at) "
            "VALUES (%s, 'image/png', 'x', %s)",
            (sha, last_used_at),
        )
        return sha

    def _exists(self, table: str, column: str, value: str) -> bool:
        return bool(self._sql(f"SELECT 1 FROM {table} WHERE {column} = %s", (value,)))

    async def test_purge_deletes_only_what_finished_before_the_cutoff(self) -> None:
        held_blob = self._blob("held", OLD)
        freed_blob = self._blob("freed", OLD)
        loose_blob = self._blob("loose", OLD)
        fresh_blob = self._blob("fresh", RECENT)

        old_done = self._request("old-done", "completed", OLD, [freed_blob])
        old_failed = self._request("old-failed", "failed", OLD)
        old_running = self._request("old-running", "running", OLD, [held_blob])
        new_done = self._request("new-done", "completed", RECENT)
        self._event(None, None, OLD)
        self._event(None, None, RECENT)

        old_run = self._run("old-run", "completed", OLD)
        kept_by_eval = self._run("eval-run", "failed", OLD)
        running_run = self._run("running-run", "running", None)
        new_run = self._run("new-run", "completed", RECENT)
        eval_id = f"{self.tag}-eval"
        self._sql(
            "INSERT INTO eval (eval_id, host_id, dataset_id, rubric_id) VALUES (%s, 'h', 'd', 'r')",
            (eval_id,),
        )
        self._sql(
            "INSERT INTO eval_experiment (experiment_id, eval_id, workflow_run_id, agent_spec_snapshot) "
            "VALUES (%s, %s, %s, '{}'::jsonb)",
            (f"{self.tag}-exp", eval_id, kept_by_eval),
        )

        counts = await purge(self.pool, cutoff=CUTOFF)

        for request_id in (old_done, old_failed):
            self.assertFalse(self._exists("runtime_request", "request_id", request_id))
            self.assertFalse(self._exists("runtime_request_seq", "request_id", request_id))
            self.assertFalse(self._exists("runtime_event_log", "request_id", request_id))
        for request_id in (old_running, new_done):
            self.assertTrue(self._exists("runtime_request", "request_id", request_id))
            self.assertTrue(self._exists("runtime_request_seq", "request_id", request_id))
            self.assertEqual(
                len(self._sql("SELECT 1 FROM runtime_event_log WHERE request_id = %s", (request_id,))),
                2,
            )
        unowned = self._sql(
            "SELECT created_at FROM runtime_event_log WHERE app_id = %s AND request_id IS NULL",
            (f"{self.tag}-app",),
        )
        self.assertEqual([row[0] for row in unowned], [RECENT])

        self.assertFalse(self._exists("workflow_runs", "run_id", old_run))
        self.assertFalse(self._exists("workflow_steps", "run_id", old_run))
        self.assertFalse(self._exists("workflow_step_events", "run_id", old_run))
        for run_id in (kept_by_eval, running_run, new_run):
            self.assertTrue(self._exists("workflow_runs", "run_id", run_id))
            self.assertTrue(self._exists("workflow_step_events", "run_id", run_id))

        self.assertTrue(self._exists("runtime_blob", "sha256", held_blob))
        self.assertTrue(self._exists("runtime_blob", "sha256", fresh_blob))
        self.assertFalse(self._exists("runtime_blob", "sha256", freed_blob))
        self.assertFalse(self._exists("runtime_blob", "sha256", loose_blob))

        self.assertGreaterEqual(counts["requests"], 2)
        self.assertGreaterEqual(counts["workflow_runs"], 1)

    async def test_purge_drains_more_than_one_batch(self) -> None:
        from mash.storage import retention as retention_module

        ids = [self._request(f"bulk-{n}", "completed", OLD) for n in range(7)]
        original = retention_module.OWNER_BATCH
        retention_module.OWNER_BATCH = 3
        try:
            await purge(self.pool, cutoff=CUTOFF)
        finally:
            retention_module.OWNER_BATCH = original
        for request_id in ids:
            self.assertFalse(self._exists("runtime_request", "request_id", request_id))

    async def test_task_runs_a_pass_on_start_with_the_window_as_cutoff(self) -> None:
        # The real purge is replaced: a cutoff of now minus a day would reach
        # real rows in a shared database.
        from mash.storage import retention as retention_module

        cutoffs: list[float] = []
        ran = asyncio.Event()

        async def _record(pool: Any, *, cutoff: float) -> dict[str, int]:
            del pool
            cutoffs.append(cutoff)
            ran.set()
            return {"requests": 0, "unowned_events": 0, "workflow_runs": 0, "blobs": 0}

        original = retention_module.purge
        retention_module.purge = _record
        retention = Retention(self.url, days=2.0)
        try:
            before = time.time()
            await retention.start()
            await asyncio.wait_for(ran.wait(), timeout=5.0)
        finally:
            await retention.stop()
            retention_module.purge = original
        self.assertEqual(len(cutoffs), 1)
        self.assertAlmostEqual(cutoffs[0], before - 2 * 86400.0, delta=5.0)

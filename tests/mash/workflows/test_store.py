"""Integration tests for the workflow store (requires Postgres)."""

from __future__ import annotations

import os
import time
import unittest
import uuid
from dataclasses import replace
from typing import Any

from mash.workflows.store import (
    RUN_COMPLETED,
    RUN_QUEUED,
    RUN_RUNNING,
    STEP_COMPLETED,
    STEP_EVENT_COMPLETED,
    STEP_EVENT_STARTED,
    STEP_RUNNING,
    WorkflowRunRecord,
    WorkflowStepRecord,
    WorkflowStore,
)

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash",
    )


def _require_postgres() -> str:
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    database_url = _database_url()
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return database_url


def _cleanup(database_url: str, run_id: str) -> None:
    assert psycopg is not None  # guaranteed by _require_postgres in setup
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM workflow_step_events WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM workflow_steps WHERE run_id = %s", (run_id,))
            cursor.execute("DELETE FROM workflow_runs WHERE run_id = %s", (run_id,))


class WorkflowStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = _require_postgres()
        self.store = WorkflowStore(self.database_url)
        await self.store.open()
        self.workflow_id = "changelog"
        self.run_id = f"mw:test:{self.workflow_id}:{uuid.uuid4().hex}"
        self.extra_run_ids: list[str] = []

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _cleanup(self.database_url, self.run_id)
        for run_id in self.extra_run_ids:
            _cleanup(self.database_url, run_id)

    def _run(self, **overrides: Any) -> WorkflowRunRecord:
        record = WorkflowRunRecord(
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            status=RUN_QUEUED,
            workflow_input={"repo_url": "https://example.com/repo"},
            created_at=time.time(),
        )
        return replace(record, **overrides) if overrides else record

    async def test_run_lifecycle(self) -> None:
        await self.store.create_run(self._run())
        fetched = await self.store.get_run(self.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RUN_QUEUED)
        self.assertEqual(fetched.workflow_input["repo_url"], "https://example.com/repo")

        await self.store.mark_run_started(self.run_id, time.time())
        fetched = await self.store.get_run(self.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RUN_RUNNING)
        self.assertIsNotNone(fetched.started_at)

        await self.store.finish_run(
            self.run_id,
            status=RUN_COMPLETED,
            result={"summary": "done", "head_sha": "abc"},
            finished_at=time.time(),
        )
        fetched = await self.store.get_run(self.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RUN_COMPLETED)
        self.assertEqual(fetched.result, {"summary": "done", "head_sha": "abc"})
        self.assertIsNotNone(fetched.finished_at)

    async def test_create_run_is_idempotent(self) -> None:
        await self.store.create_run(self._run())
        # A DBOS replay may re-attempt the insert; it must not raise or clobber.
        await self.store.create_run(self._run(status=RUN_RUNNING))
        fetched = await self.store.get_run(self.run_id)
        assert fetched is not None
        self.assertEqual(fetched.status, RUN_QUEUED)

    async def test_step_upsert_threads_snapshots(self) -> None:
        await self.store.create_run(self._run())
        await self.store.upsert_step(
            WorkflowStepRecord(
                run_id=self.run_id,
                workflow_id=self.workflow_id,
                step_id="scan",
                ordinal=0,
                kind="code",
                status=STEP_RUNNING,
                input_snapshot={"repo_url": "https://example.com/repo"},
                started_at=time.time(),
            )
        )
        await self.store.upsert_step(
            WorkflowStepRecord(
                run_id=self.run_id,
                workflow_id=self.workflow_id,
                step_id="scan",
                ordinal=0,
                kind="code",
                status=STEP_COMPLETED,
                input_snapshot={"repo_url": "https://example.com/repo"},
                output_snapshot={"head_sha": "abc", "files_changed": ["a.py"]},
                finished_at=time.time(),
            )
        )
        steps = await self.store.get_run_steps(self.run_id)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].status, STEP_COMPLETED)
        self.assertEqual(
            steps[0].output_snapshot,
            {"head_sha": "abc", "files_changed": ["a.py"]},
        )
        # started_at is preserved across the upsert.
        self.assertIsNotNone(steps[0].started_at)

    async def test_step_events_seq_is_monotonic(self) -> None:
        await self.store.create_run(self._run())
        seq1 = await self.store.append_step_event(
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            step_id="scan",
            event_type=STEP_EVENT_STARTED,
            at=time.time(),
        )
        seq2 = await self.store.append_step_event(
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            step_id="scan",
            event_type=STEP_EVENT_COMPLETED,
            at=time.time(),
            payload={"ok": True},
        )
        self.assertEqual((seq1, seq2), (1, 2))
        events = await self.store.list_step_events(self.run_id, step_id="scan")
        self.assertEqual([e.event_type for e in events], [STEP_EVENT_STARTED, STEP_EVENT_COMPLETED])
        after = await self.store.list_step_events(self.run_id, step_id="scan", after_seq=1)
        self.assertEqual([e.seq for e in after], [2])

    async def test_list_runs_filters_and_orders(self) -> None:
        await self.store.create_run(self._run())
        runs = await self.store.list_runs(self.workflow_id, limit=50)
        self.assertTrue(any(r.run_id == self.run_id for r in runs))
        completed = await self.store.list_runs(self.workflow_id, status=RUN_COMPLETED)
        self.assertFalse(any(r.run_id == self.run_id for r in completed))

    async def test_get_latest_runs_returns_newest_run_per_workflow(self) -> None:
        await self.store.create_run(self._run(created_at=1.0))
        newer_run_id = f"mw:test:{self.workflow_id}:{uuid.uuid4().hex}"
        self.extra_run_ids.append(newer_run_id)
        await self.store.create_run(
            self._run(run_id=newer_run_id, created_at=2.0, status=RUN_COMPLETED)
        )

        latest = await self.store.get_latest_runs([self.workflow_id, "missing"])

        self.assertEqual(latest[self.workflow_id].run_id, newer_run_id)
        self.assertNotIn("missing", latest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

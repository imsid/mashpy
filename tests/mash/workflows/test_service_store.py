"""Store-backed WorkflowService behavior for step pipelines (requires Postgres)."""

from __future__ import annotations

import os
import time
import unittest
import uuid
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from pydantic import BaseModel

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool

from mash.workflows import (
    CodeStep,
    StepContext,
    WorkflowRegistry,
    WorkflowService,
    WorkflowSpec,
)
from mash.workflows.engine import WorkflowRunner
from mash.workflows.service import (
    WorkflowNotFoundError,
    WorkflowResumeNotSupportedError,
)
from mash.workflows.store import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RunDeduplicatedError,
    STEP_COMPLETED,
    STEP_EVENT_STARTED,
    STEP_FAILED,
    WorkflowRunRecord,
    WorkflowStepRecord,
    WorkflowStore,
)

try:  # pragma: no cover
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )


def _require_postgres() -> str:
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    url = _database_url()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


def _cleanup(url: str, workflow_id: str) -> None:
    assert psycopg is not None  # guaranteed by _require_postgres in setup
    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM workflow_step_events WHERE workflow_id = %s",
                (workflow_id,),
            )
            cursor.execute(
                "DELETE FROM workflow_steps WHERE workflow_id = %s", (workflow_id,)
            )
            cursor.execute(
                "DELETE FROM workflow_runs WHERE workflow_id = %s", (workflow_id,)
            )


class TriggerIn(BaseModel):
    n: int


class Out(BaseModel):
    doubled: int


def _double(
    inp: TriggerIn, _ctx: StepContext
) -> Out:  # pragma: no cover - not executed here
    return Out(doubled=inp.n * 2)


class _FakePool:
    def __init__(self, store: WorkflowStore) -> None:
        self.runtime_database_url = "postgresql://example"
        self._store = store

    def get_workflow_store(self) -> WorkflowStore:
        return self._store


class WorkflowServiceStoreTests(unittest.IsolatedAsyncioTestCase):
    WF = "svc-pipe"

    async def asyncSetUp(self) -> None:
        self.started: list[str] = []
        starter = patch.object(
            WorkflowRunner, "start", lambda _r, run: self.started.append(run.run_id)
        )
        starter.start()
        self.addCleanup(starter.stop)
        self.url = _require_postgres()
        self.store = WorkflowStore(self.url)
        await self.store.open()
        self.registry = WorkflowRegistry()
        self.registry.register(
            WorkflowSpec(
                workflow_id=self.WF,
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=Out)
                ],
            )
        )
        self.service = WorkflowService(
            self.registry,
            cast("Pool", _FakePool(self.store)),
            runner_id="svc-runner",
        )
        self.run_id = f"mw:svc:{self.WF}:{uuid.uuid4().hex}"

    async def asyncTearDown(self) -> None:
        await self.store.close()
        _cleanup(self.url, self.WF)

    async def _seed(
        self, status: str, *, result: dict | None = None, error: str | None = None
    ) -> None:
        now = time.time()
        await self.store.create_run(
            WorkflowRunRecord(
                run_id=self.run_id,
                workflow_id=self.WF,
                status=status,
                workflow_input={"n": 3},
                result=result,
                error=error,
                created_at=now,
                started_at=now,
                finished_at=now if status in (RUN_COMPLETED, RUN_FAILED) else None,
            )
        )
        await self.store.upsert_step(
            WorkflowStepRecord(
                run_id=self.run_id,
                workflow_id=self.WF,
                step_id="double",
                ordinal=0,
                kind="code",
                status=STEP_COMPLETED if status == RUN_COMPLETED else STEP_FAILED,
                input_snapshot={"n": 3},
                output_snapshot={"doubled": 6} if status == RUN_COMPLETED else None,
                error=error,
                finished_at=now,
            )
        )
        await self.store.append_step_event(
            run_id=self.run_id,
            workflow_id=self.WF,
            step_id="double",
            event_type=STEP_EVENT_STARTED,
            at=now,
        )

    async def test_list_runs_reads_store(self) -> None:
        await self._seed(RUN_COMPLETED, result={"doubled": 6})
        runs = await self.service.list_runs(self.WF)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run_id, self.run_id)
        self.assertEqual(runs[0].status, RUN_COMPLETED)
        self.assertEqual(runs[0].result, {"doubled": 6})

    async def test_list_runs_status_filter(self) -> None:
        await self._seed(RUN_COMPLETED, result={"doubled": 6})
        self.assertEqual(
            len(await self.service.list_runs(self.WF, status=RUN_FAILED)), 0
        )

    async def test_get_run_includes_steps(self) -> None:
        await self._seed(RUN_COMPLETED, result={"doubled": 6})
        run = await self.service.get_run(self.WF, self.run_id)
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.workflow_input, {"n": 3})
        self.assertEqual(run.result, {"doubled": 6})
        assert run.steps is not None
        self.assertEqual(len(run.steps), 1)
        self.assertEqual(run.steps[0]["output_snapshot"], {"doubled": 6})

    async def test_get_run_missing_raises(self) -> None:
        with self.assertRaises(WorkflowNotFoundError):
            await self.service.get_run(self.WF, "mw:svc:svc-pipe:missing")

    async def test_list_run_step_events(self) -> None:
        await self._seed(RUN_COMPLETED, result={"doubled": 6})
        events = await self.service.list_run_step_events(self.WF, self.run_id)
        self.assertEqual([e["event_type"] for e in events], [STEP_EVENT_STARTED])

    async def test_resume_leaves_a_running_run_running(self) -> None:
        await self._seed(RUN_RUNNING)
        run = await self.service.resume_run(self.WF, self.run_id)
        self.assertEqual(run.status, RUN_RUNNING)
        self.assertEqual(self.started, [self.run_id])

    async def test_resume_requeues_a_failed_run(self) -> None:
        await self._seed(RUN_FAILED, error="boom")
        run = await self.service.resume_run(self.WF, self.run_id)
        self.assertEqual(run.status, RUN_QUEUED)
        self.assertIsNone(run.error)
        self.assertIsNone(run.finished_at)
        self.assertEqual(self.started, [self.run_id])

    async def test_resume_rejects_a_completed_run(self) -> None:
        await self._seed(RUN_COMPLETED, result={"doubled": 6})
        with self.assertRaises(WorkflowResumeNotSupportedError) as caught:
            await self.service.resume_run(self.WF, self.run_id)
        self.assertEqual(caught.exception.status, RUN_COMPLETED)
        self.assertEqual(self.started, [])

    async def test_an_active_dedup_key_admits_one_run(self) -> None:
        def run(run_id: str) -> WorkflowRunRecord:
            return WorkflowRunRecord(
                run_id=run_id,
                workflow_id=self.WF,
                status=RUN_QUEUED,
                dedup_key="nightly",
                created_at=time.time(),
            )

        await self.store.create_run(run(self.run_id))
        with self.assertRaises(RunDeduplicatedError) as caught:
            await self.store.create_run(run(f"{self.run_id}-2"))
        self.assertEqual(caught.exception.run_id, self.run_id)

        await self.store.finish_run(self.run_id, status=RUN_COMPLETED, finished_at=time.time())
        await self.store.create_run(run(f"{self.run_id}-3"))

    async def test_unfinished_runs_are_listed_by_runner_prefix(self) -> None:
        mine = f"mw:svc-a:{self.WF}:{uuid.uuid4().hex}"
        other = f"mw:svc-b:{self.WF}:{uuid.uuid4().hex}"
        for run_id in (mine, other):
            await self.store.create_run(
                WorkflowRunRecord(
                    run_id=run_id, workflow_id=self.WF, status=RUN_RUNNING, created_at=time.time()
                )
            )
        listed = await self.store.list_unfinished_runs("mw:svc-a:")
        self.assertEqual([r.run_id for r in listed], [mine])
        self.assertEqual(await self.store.count_recovery(mine), 1)
        self.assertEqual(await self.store.count_recovery(mine), 2)

    async def test_resume_missing_run_raises(self) -> None:
        with self.assertRaises(WorkflowNotFoundError):
            await self.service.resume_run(self.WF, "mw:svc:svc-pipe:missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

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
    from mash.runtime.host.host import AgentPool

from mash.workflows import (
    CodeStep,
    StepContext,
    WorkflowRegistry,
    WorkflowService,
    WorkflowSpec,
)
from mash.workflows import dbos as workflow_dbos
from mash.workflows.service import WorkflowNotFoundError
from mash.workflows.store import (
    RUN_COMPLETED,
    RUN_FAILED,
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
            cast("AgentPool", _FakePool(self.store)),
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

    async def test_resume_run_invokes_dbos_and_returns_run(self) -> None:
        await self._seed(RUN_FAILED, error="boom")
        with patch.object(
            workflow_dbos, "resume_workflow_run", return_value=self.run_id
        ) as resume:
            run = await self.service.resume_run(self.WF, self.run_id)
        resume.assert_awaited_once_with(self.run_id)
        self.assertEqual(run.run_id, self.run_id)

    async def test_resume_missing_run_raises(self) -> None:
        with self.assertRaises(WorkflowNotFoundError):
            await self.service.resume_run(self.WF, "mw:svc:svc-pipe:missing")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

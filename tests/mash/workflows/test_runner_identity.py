"""Runner identity: a host recovers the unfinished runs of its own deployment."""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from conftest import _TestWorkflowStore
from mash.workflows import CodeStep, StepContext, WorkflowRegistry, WorkflowSpec
from mash.workflows.engine import MAX_RECOVERIES, WorkflowRunner
from mash.workflows.runs import (
    DEFAULT_RUNNER_ID,
    MASH_RUNNER_ID_ENV,
    make_run_id,
    resolve_runner_id,
)
from mash.workflows.store import RUN_COMPLETED, RUN_FAILED, RUN_QUEUED, WorkflowRunRecord


class ResolveRunnerIdTests(unittest.TestCase):
    def test_default_is_stable_across_calls(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MASH_RUNNER_ID_ENV, None)
            self.assertEqual(resolve_runner_id(), DEFAULT_RUNNER_ID)
            self.assertEqual(resolve_runner_id(), resolve_runner_id())

    def test_environment_overrides_the_default(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "blue"}):
            self.assertEqual(resolve_runner_id(), "blue")

    def test_explicit_value_wins_over_the_environment(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "blue"}):
            self.assertEqual(resolve_runner_id("green"), "green")

    def test_a_colon_is_rejected_because_run_ids_are_colon_delimited(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runner_id("blue:green")

    def test_blank_falls_back_rather_than_producing_an_empty_id(self) -> None:
        with patch.dict(os.environ, {MASH_RUNNER_ID_ENV: "   "}):
            self.assertEqual(resolve_runner_id("  "), DEFAULT_RUNNER_ID)


class _In(BaseModel):
    n: int


class _Out(BaseModel):
    n: int


class _Pool:
    def __init__(self) -> None:
        self.registry = WorkflowRegistry()
        self.store = _TestWorkflowStore("postgresql://test/runtime")
        self.ran: list[str] = []

        def _step(inp: _In, ctx: StepContext) -> _Out:
            self.ran.append(ctx.run_id)
            return _Out(n=inp.n)

        self.registry.register(
            WorkflowSpec(
                workflow_id="wf",
                input_model=_In,
                steps=[CodeStep(step_id="only", run=_step, input=_In, output=_Out)],
            )
        )

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self.registry

    def get_workflow_store(self) -> Any:
        return self.store


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _queue(self, pool: _Pool, runner_id: str, *, status: str = RUN_QUEUED) -> str:
        run_id = make_run_id(runner_id, "wf")
        await pool.store.create_run(
            WorkflowRunRecord(
                run_id=run_id,
                workflow_id="wf",
                status=status,
                workflow_input={"n": 1},
                created_at=1.0,
            )
        )
        return run_id

    async def _recover(self, pool: _Pool, runner_id: str) -> None:
        runner = WorkflowRunner(pool, runner_id=runner_id)  # type: ignore[arg-type]
        await runner.recover()
        for _ in range(50):
            if not runner._tasks:
                break
            await asyncio.sleep(0.01)
        await runner.close()

    async def test_a_restarted_host_finishes_its_unfinished_runs(self) -> None:
        pool = _Pool()
        queued = await self._queue(pool, "deploy-a")
        running = await self._queue(pool, "deploy-a", status="running")

        await self._recover(pool, "deploy-a")

        self.assertCountEqual(pool.ran, [queued, running])
        for run_id in (queued, running):
            run = await pool.store.get_run(run_id)
            self.assertEqual(run.status, RUN_COMPLETED)

    async def test_separate_deployments_stay_isolated(self) -> None:
        pool = _Pool()
        blue = await self._queue(pool, "blue")
        green = await self._queue(pool, "green")

        await self._recover(pool, "blue")

        self.assertEqual(pool.ran, [blue])
        self.assertEqual((await pool.store.get_run(green)).status, RUN_QUEUED)

    async def test_a_run_past_the_recovery_limit_fails(self) -> None:
        pool = _Pool()
        run_id = await self._queue(pool, "blue")
        pool.store._recoveries[run_id] = MAX_RECOVERIES

        await self._recover(pool, "blue")

        run = await pool.store.get_run(run_id)
        self.assertEqual(pool.ran, [])
        self.assertEqual(run.status, RUN_FAILED)
        self.assertIn("recoveries", run.error)

    async def test_a_run_of_an_unregistered_workflow_fails(self) -> None:
        pool = _Pool()
        run_id = make_run_id("blue", "gone")
        await pool.store.create_run(
            WorkflowRunRecord(run_id=run_id, workflow_id="gone", status=RUN_QUEUED, created_at=1.0)
        )

        await self._recover(pool, "blue")

        run = await pool.store.get_run(run_id)
        self.assertEqual(run.status, RUN_FAILED)
        self.assertIn("no longer registered", run.error)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

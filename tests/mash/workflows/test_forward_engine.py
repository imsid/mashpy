"""End-to-end tests for the forward-pipeline engine (requires Postgres).

Drives ``execute_registered_workflow`` with a fake DBOS (steps run inline) and a
real ``WorkflowStore``, asserting output threading, result persistence, the step
audit trail, and failure handling.
"""

from __future__ import annotations

import inspect
import os
import unittest
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

from pydantic import BaseModel

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentPool

from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowRegistry, WorkflowSpec
from mash.workflows import dbos as workflow_dbos
from mash.workflows.dbos import make_run_id
from mash.workflows.store import (
    RUN_COMPLETED,
    RUN_FAILED,
    STEP_COMPLETED,
    STEP_EVENT_COMPLETED,
    STEP_EVENT_FAILED,
    STEP_EVENT_STARTED,
    STEP_FAILED,
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
                "DELETE FROM workflow_step_events WHERE workflow_id = %s", (workflow_id,)
            )
            cursor.execute("DELETE FROM workflow_steps WHERE workflow_id = %s", (workflow_id,))
            cursor.execute("DELETE FROM workflow_runs WHERE workflow_id = %s", (workflow_id,))


# --- Pipeline models and steps ----------------------------------------------


class TriggerIn(BaseModel):
    n: int


class DoubleOut(BaseModel):
    n: int
    doubled: int


class FinalOut(BaseModel):
    doubled: int
    message: str


def _double(inp: TriggerIn, _ctx: StepContext) -> DoubleOut:
    return DoubleOut(n=inp.n, doubled=inp.n * 2)


def _finalize(inp: DoubleOut, ctx: StepContext) -> FinalOut:
    return FinalOut(doubled=inp.doubled, message=f"run={ctx.run_id[:6]}")


def _boom(_inp: DoubleOut, _ctx: StepContext) -> FinalOut:
    raise ValueError("kaboom")


class _FakeDBOS:
    step_names: list[str] = []

    @staticmethod
    async def run_step_async(config, func, *args, **kwargs):
        _FakeDBOS.step_names.append(config["name"])
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class _FakeHost:
    def __init__(self, registry: WorkflowRegistry, store: WorkflowStore) -> None:
        self.runtime_database_url = "postgresql://example"
        self._registry = registry
        self._store = store

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._registry

    def get_workflow_store(self) -> WorkflowStore:
        return self._store


class ForwardPipelineEngineTests(unittest.IsolatedAsyncioTestCase):
    RUNNER = "runner-engine"

    async def asyncSetUp(self) -> None:
        _FakeDBOS.step_names = []
        self.url = _require_postgres()
        self.store = WorkflowStore(self.url)
        await self.store.open()
        self.registry = WorkflowRegistry()
        self.host = cast("AgentPool", _FakeHost(self.registry, self.store))
        workflow_dbos.register_runner(self.RUNNER, self.host)

    async def asyncTearDown(self) -> None:
        workflow_dbos.unregister_runner(self.RUNNER, self.host)
        await self.store.close()
        for wf in ("pipe", "pipe-fail", "pipe-agent", "pipe-orchestration"):
            _cleanup(self.url, wf)

    async def _execute(self, workflow_id: str, workflow_input: dict[str, Any]) -> tuple[str, Any]:
        run_id = make_run_id(self.RUNNER, workflow_id)
        with patch.object(
            workflow_dbos, "_load_dbos_api", return_value=(_FakeDBOS, None, None, None, None)
        ), patch(
            "mash.workflows.engine.load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            output = await workflow_dbos.execute_registered_workflow(
                self.RUNNER, workflow_id, run_id, workflow_input=workflow_input
            )
        return run_id, output

    async def test_code_pipeline_threads_and_persists(self) -> None:
        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=DoubleOut),
                    CodeStep(step_id="finalize", run=_finalize, input=DoubleOut, output=FinalOut),
                ],
            )
        )
        run_id, output = await self._execute("pipe", {"n": 21})

        self.assertEqual(output["result"]["doubled"], 42)
        self.assertTrue(output["result"]["message"].startswith("run="))

        run = await self.store.get_run(run_id)
        assert run is not None
        assert run.result is not None
        self.assertEqual(run.status, RUN_COMPLETED)
        self.assertEqual(run.result["doubled"], 42)

        steps = await self.store.get_run_steps(run_id)
        self.assertEqual([s.step_id for s in steps], ["double", "finalize"])
        self.assertTrue(all(s.status == STEP_COMPLETED for s in steps))
        # Output of step 1 threaded into step 2's input snapshot.
        self.assertEqual(steps[0].output_snapshot, {"n": 21, "doubled": 42})
        self.assertEqual(steps[1].input_snapshot, {"n": 21, "doubled": 42})

        events = await self.store.list_step_events(run_id, step_id="double")
        self.assertEqual([e.event_type for e in events], [STEP_EVENT_STARTED, STEP_EVENT_COMPLETED])

    async def test_orchestration_code_runs_in_workflow_context(self) -> None:
        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-orchestration",
                input_model=TriggerIn,
                steps=[
                    CodeStep(
                        step_id="double",
                        run=_double,
                        input=TriggerIn,
                        output=DoubleOut,
                    ),
                    CodeStep(
                        step_id="fan-out",
                        run=_finalize,
                        input=DoubleOut,
                        output=FinalOut,
                        orchestration=True,
                    ),
                ],
            )
        )

        _, output = await self._execute("pipe-orchestration", {"n": 3})

        self.assertEqual(output["result"]["doubled"], 6)
        self.assertIn("double.run", _FakeDBOS.step_names)
        self.assertNotIn("fan-out.run", _FakeDBOS.step_names)

    async def test_failed_step_marks_run_failed(self) -> None:
        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-fail",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=DoubleOut),
                    CodeStep(step_id="boom", run=_boom, input=DoubleOut, output=FinalOut),
                ],
            )
        )
        with self.assertRaises(ValueError):
            await self._execute("pipe-fail", {"n": 5})

        runs = await self.store.list_runs("pipe-fail", status=RUN_FAILED)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.status, RUN_FAILED)
        self.assertIn("kaboom", run.error or "")

        steps = await self.store.get_run_steps(run.run_id)
        by_id = {s.step_id: s for s in steps}
        self.assertEqual(by_id["double"].status, STEP_COMPLETED)
        self.assertEqual(by_id["boom"].status, STEP_FAILED)
        fail_events = await self.store.list_step_events(run.run_id, step_id="boom")
        self.assertIn(STEP_EVENT_FAILED, [e.event_type for e in fail_events])

    async def test_agent_step_uses_structured_output(self) -> None:
        async def _fake_post(runner_id, *, agent_id, message, structured_output, **kwargs):
            del runner_id, agent_id, message, kwargs
            # The agent step's output model becomes the request's schema.
            self.assertIn("doubled", structured_output.get("properties", {}))
            return "req-1"

        async def _fake_collect(runner_id, agent_id, request_id):
            del runner_id, agent_id, request_id
            return {"response": {"structured_output": {"doubled": 84, "message": "from-agent"}}}

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-agent",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=DoubleOut),
                    AgentStep(
                        step_id="write", agent_id="writer", input=DoubleOut, output=FinalOut
                    ),
                ],
            )
        )
        with patch("mash.workflows.engine.post_inline_agent_request", _fake_post), patch(
            "mash.workflows.engine.collect_terminal_payload", _fake_collect
        ):
            run_id, output = await self._execute("pipe-agent", {"n": 42})

        self.assertEqual(output["result"], {"doubled": 84, "message": "from-agent"})
        steps = await self.store.get_run_steps(run_id)
        write = next(s for s in steps if s.step_id == "write")
        self.assertEqual(write.status, STEP_COMPLETED)
        self.assertEqual(write.agent_request_id, "req-1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

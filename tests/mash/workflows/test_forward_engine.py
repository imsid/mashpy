"""End-to-end tests for the forward-pipeline engine (requires Postgres).

Drives the pipeline over a real ``WorkflowStore``, asserting output threading,
result persistence, the step audit trail, failure handling, and resuming a run
from its completed steps.
"""

from __future__ import annotations

import os
import unittest
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool

from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowRegistry, WorkflowSpec
from mash.workflows.engine import FORWARD_PIPELINE_STRATEGY, WorkflowExecutionContext
from mash.workflows.runs import make_run_id
from mash.workflows.store import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    STEP_RUNNING,
    STEP_COMPLETED,
    STEP_EVENT_COMPLETED,
    STEP_EVENT_FAILED,
    STEP_EVENT_STARTED,
    STEP_FAILED,
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
        self.url = _require_postgres()
        self.store = WorkflowStore(self.url)
        await self.store.open()
        self.registry = WorkflowRegistry()
        self.host = cast("Pool", _FakeHost(self.registry, self.store))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        for wf in (
            "pipe",
            "pipe-fail",
            "pipe-agent",
            "pipe-agent-exhausted",
            "pipe-agent-fail",
            "pipe-coerce-fail",
            "pipe-resume",
            "pipe-crash",
        ):
            _cleanup(self.url, wf)

    async def _execute(
        self, workflow_id: str, workflow_input: dict[str, Any], run_id: str | None = None
    ) -> tuple[str, Any]:
        """Run the pipeline once, creating the run's queued row on first use."""
        run_id = run_id or make_run_id(self.RUNNER, workflow_id)
        if await self.store.get_run(run_id) is None:
            await self.store.create_run(
                WorkflowRunRecord(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    status=RUN_QUEUED,
                    workflow_input=workflow_input,
                    user_id="u-1",
                    created_at=1.0,
                )
            )
        output = await FORWARD_PIPELINE_STRATEGY.run(
            WorkflowExecutionContext(
                pool=self.host,
                workflow=self.registry.get(workflow_id),
                run_id=run_id,
                workflow_input=workflow_input,
                user_id="u-1",
            )
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

    async def test_resume_reruns_from_the_failed_step(self) -> None:
        calls: list[tuple[str, int]] = []
        fail = {"boom": True}

        def _double_counted(inp: TriggerIn, ctx: StepContext) -> DoubleOut:
            calls.append((ctx.step_id, ctx.attempt))
            return _double(inp, ctx)

        def _flaky(inp: DoubleOut, ctx: StepContext) -> FinalOut:
            calls.append((ctx.step_id, ctx.attempt))
            if fail["boom"]:
                raise ValueError("kaboom")
            return _finalize(inp, ctx)

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-resume",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double_counted, input=TriggerIn, output=DoubleOut),
                    CodeStep(step_id="finalize", run=_flaky, input=DoubleOut, output=FinalOut),
                ],
            )
        )
        with self.assertRaises(ValueError):
            run_id, _ = await self._execute("pipe-resume", {"n": 4})
        run_id = (await self.store.list_runs("pipe-resume"))[0].run_id

        fail["boom"] = False
        await self.store.requeue_run(run_id)
        _, output = await self._execute("pipe-resume", {"n": 4}, run_id=run_id)

        self.assertEqual(output["result"]["doubled"], 8)
        # The completed step is not run again; the failed one runs as attempt 2.
        self.assertEqual(calls, [("double", 1), ("finalize", 1), ("finalize", 2)])
        events = await self.store.list_step_events(run_id, step_id="finalize")
        self.assertEqual(
            [(e.attempt, e.event_type) for e in events],
            [
                (1, STEP_EVENT_STARTED),
                (1, STEP_EVENT_FAILED),
                (2, STEP_EVENT_STARTED),
                (2, STEP_EVENT_COMPLETED),
            ],
        )
        run = await self.store.get_run(run_id)
        assert run is not None
        self.assertEqual(run.status, RUN_COMPLETED)

    async def test_step_the_host_died_in_keeps_its_agent_request(self) -> None:
        submitted: list[str] = []

        async def _fake_post(pool, *, agent_id, message, structured_output, **kwargs):
            del pool, agent_id, message, structured_output
            from mash.workflows.agents import agent_step_request_id

            request_id = agent_step_request_id(
                kwargs["workflow_run_id"], kwargs["task_id"], kwargs["attempt"]
            )
            submitted.append(request_id)
            return request_id

        async def _fake_collect(pool, agent_id, request_id):
            del pool, agent_id, request_id
            return {"response": {"structured_output": {"doubled": 2, "message": "ok"}}}

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-crash",
                input_model=TriggerIn,
                steps=[
                    AgentStep(step_id="write", agent_id="writer", input=None, output=FinalOut),
                ],
            )
        )
        run_id = make_run_id(self.RUNNER, "pipe-crash")
        await self.store.create_run(
            WorkflowRunRecord(
                run_id=run_id,
                workflow_id="pipe-crash",
                status="running",
                workflow_input={"n": 1},
                created_at=1.0,
            )
        )
        # The host died after the step started its request.
        await self.store.upsert_step(
            WorkflowStepRecord(
                run_id=run_id,
                workflow_id="pipe-crash",
                step_id="write",
                ordinal=0,
                kind="agent",
                status=STEP_RUNNING,
                attempt=1,
                agent_request_id=f"{run_id}-write",
            )
        )
        with patch("mash.workflows.engine.post_inline_agent_request", _fake_post), patch(
            "mash.workflows.engine.collect_terminal_payload", _fake_collect
        ):
            await self._execute("pipe-crash", {"n": 1}, run_id=run_id)

        self.assertEqual(submitted, [f"{run_id}-write"])
        steps = {step.step_id: step for step in await self.store.get_run_steps(run_id)}
        self.assertEqual(steps["write"].attempt, 1)
        self.assertEqual(steps["write"].status, STEP_COMPLETED)

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

    async def test_agent_step_budget_exhaustion_stops_the_pipeline(self) -> None:
        # Issue #188: an agent that burned its whole step budget fails its
        # request. The step must fail with it rather than thread a synthesized
        # result into the next step.
        ran_after: list[str] = []

        def _after(inp: FinalOut, ctx: StepContext) -> FinalOut:  # pragma: no cover
            ran_after.append(ctx.step_id)
            return inp

        async def _fake_post(runner_id, *, agent_id, message, structured_output, **kwargs):
            del runner_id, agent_id, message, structured_output, kwargs
            return "req-exhausted"

        async def _fake_collect(runner_id, agent_id, request_id):
            del runner_id, agent_id, request_id
            # What collect_terminal_payload raises on a request.error frame.
            raise RuntimeError(
                "Stopped after reaching the max step limit (60) before "
                "finishing. Increase `max_steps` or narrow the task."
            )

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-agent-exhausted",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=DoubleOut),
                    AgentStep(
                        step_id="write", agent_id="writer", input=DoubleOut, output=FinalOut
                    ),
                    CodeStep(step_id="after", run=_after, input=FinalOut, output=FinalOut),
                ],
            )
        )
        run_id = make_run_id(self.RUNNER, "pipe-agent-exhausted")
        with patch("mash.workflows.engine.post_inline_agent_request", _fake_post), patch(
            "mash.workflows.engine.collect_terminal_payload", _fake_collect
        ):
            with self.assertRaises(RuntimeError) as caught:
                await self._execute("pipe-agent-exhausted", {"n": 42}, run_id=run_id)

        self.assertIn("max step limit", str(caught.exception))
        self.assertEqual(ran_after, [])

        run = await self.store.get_run(run_id)
        assert run is not None
        self.assertEqual(run.status, RUN_FAILED)
        self.assertIsNone(run.result)

        steps = {step.step_id: step for step in await self.store.get_run_steps(run_id)}
        self.assertEqual(steps["double"].status, STEP_COMPLETED)
        self.assertEqual(steps["write"].status, STEP_FAILED)
        self.assertNotIn("after", steps)

    async def test_failed_agent_step_keeps_its_request_id(self) -> None:
        # The link from a step to the request that did its work is worth most
        # on the failure, which is exactly where it used to be erased.
        async def _fake_post(runner_id, *, agent_id, message, structured_output, **kwargs):
            del runner_id, agent_id, message, structured_output, kwargs
            return "req-failed-1"

        async def _fake_collect(runner_id, agent_id, request_id):
            del runner_id, agent_id, request_id
            raise RuntimeError("agent request failed")

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-agent-fail",
                input_model=TriggerIn,
                steps=[
                    CodeStep(step_id="double", run=_double, input=TriggerIn, output=DoubleOut),
                    AgentStep(
                        step_id="write", agent_id="writer", input=DoubleOut, output=FinalOut
                    ),
                ],
            )
        )
        run_id = make_run_id(self.RUNNER, "pipe-agent-fail")
        with patch("mash.workflows.engine.post_inline_agent_request", _fake_post), patch(
            "mash.workflows.engine.collect_terminal_payload", _fake_collect
        ):
            with self.assertRaises(RuntimeError):
                await self._execute("pipe-agent-fail", {"n": 21}, run_id=run_id)

        steps = {step.step_id: step for step in await self.store.get_run_steps(run_id)}
        self.assertEqual(steps["write"].status, STEP_FAILED)
        self.assertEqual(steps["write"].agent_request_id, "req-failed-1")

    async def test_input_coercion_failure_does_not_inherit_a_request_id(self) -> None:
        # A step that fails before starting any request has no request of its
        # own, and must not be stamped with the previous step's.
        async def _fake_post(runner_id, *, agent_id, message, structured_output, **kwargs):
            del runner_id, agent_id, message, structured_output, kwargs
            return "req-prev-step"

        async def _fake_collect(runner_id, agent_id, request_id):
            del runner_id, agent_id, request_id
            # Output that cannot coerce into the next step's input model.
            return {"response": {"structured_output": {"doubled": 84, "message": "ok"}}}

        # The field is available from the previous step's output, so the
        # pipeline builds; the value fails the constraint at run time.
        class StrictDoubled(BaseModel):
            doubled: int = Field(ge=1000)

        self.registry.register(
            WorkflowSpec(
                workflow_id="pipe-coerce-fail",
                input_model=TriggerIn,
                steps=[
                    AgentStep(
                        step_id="write", agent_id="writer", input=None, output=FinalOut
                    ),
                    CodeStep(
                        step_id="strict",
                        run=lambda inp, ctx: inp,
                        input=StrictDoubled,
                        output=StrictDoubled,
                    ),
                ],
            )
        )
        run_id = make_run_id(self.RUNNER, "pipe-coerce-fail")
        with patch("mash.workflows.engine.post_inline_agent_request", _fake_post), patch(
            "mash.workflows.engine.collect_terminal_payload", _fake_collect
        ):
            with self.assertRaises(Exception):
                await self._execute("pipe-coerce-fail", {"n": 21}, run_id=run_id)

        steps = {step.step_id: step for step in await self.store.get_run_steps(run_id)}
        self.assertEqual(steps["write"].agent_request_id, "req-prev-step")
        self.assertEqual(steps["strict"].status, STEP_FAILED)
        self.assertIsNone(steps["strict"].agent_request_id)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()

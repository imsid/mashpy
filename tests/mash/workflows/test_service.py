"""Tests for the workflow registry and service (in-memory store, no Postgres)."""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

from pydantic import BaseModel

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool

from mash.testing.runtime_fixtures import build_spec
from mash.workflows import (
    AgentStep,
    CodeStep,
    DuplicateWorkflowRunError,
    StepContext,
    WorkflowInputValidationError,
    WorkflowNotFoundError,
    WorkflowRegistry,
    WorkflowService,
    WorkflowSpec,
    WorkflowUserRequiredError,
)
from conftest import _TestWorkflowStore
from mash.workflows.engine import WorkflowRunner
from mash.workflows.runs import make_run_id, workflow_run_id_prefix


def _agent_step(step_id: str, agent_id: str) -> AgentStep:
    return AgentStep(
        step_id=step_id,
        agent_spec=build_spec(agent_id=agent_id, response_text="{}"),
        output={"type": "object"},
    )


class TriggerInput(BaseModel):
    count: int


class StepOutput(BaseModel):
    doubled: int


def _double(inp: TriggerInput, _ctx: StepContext) -> StepOutput:
    return StepOutput(doubled=inp.count * 2)


class _FakeHost:
    def __init__(self, registry: WorkflowRegistry, store: Any = None) -> None:
        self._registry = registry
        self._store = store

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._registry

    def get_workflow_store(self) -> Any:
        return self._store


class WorkflowRegistryTests(unittest.TestCase):
    def test_register_get_and_list(self) -> None:
        registry = WorkflowRegistry()
        workflow = WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        registry.register(workflow)
        self.assertIs(registry.get("wf"), workflow)
        self.assertEqual([w.workflow_id for w in registry.list()], ["wf"])

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = WorkflowRegistry()
        registry.register(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))
        with self.assertRaises(ValueError):
            registry.register(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))

    def test_workflow_without_steps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowSpec(workflow_id="wf")


class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Runs are recorded, not executed: the engine is covered elsewhere.
        self.started: list[Any] = []
        starter = patch.object(WorkflowRunner, "start", lambda _r, run: self.started.append(run))
        starter.start()
        self.addCleanup(starter.stop)

    def _service(self, workflow: WorkflowSpec, store: Any = None) -> WorkflowService:
        registry = WorkflowRegistry()
        registry.register(workflow)
        return WorkflowService(
            registry, cast("Pool", _FakeHost(registry, store)), runner_id="runner-1"
        )

    def _stored_service(self, workflow: WorkflowSpec) -> tuple[WorkflowService, Any]:
        store = _TestWorkflowStore("postgresql://test/runtime")
        return self._service(workflow, store), store

    async def test_list_workflows_returns_catalog_summaries(self) -> None:
        service = self._service(
            WorkflowSpec(
                workflow_id="wf",
                steps=[_agent_step("s1", "worker")],
                metadata={
                    "display_name": "Test workflow",
                    "description": "Runs one worker.",
                    "source": "test",
                },
            )
        )
        serialized = await service.list_workflows()
        self.assertEqual(serialized[0]["workflow_id"], "wf")
        self.assertEqual(serialized[0]["display_name"], "Test workflow")
        self.assertEqual(serialized[0]["description"], "Runs one worker.")
        self.assertEqual(serialized[0]["mode"], "pipeline")
        self.assertEqual(serialized[0]["step_count"], 1)
        self.assertEqual(serialized[0]["step_kinds"], {"code": 0, "agent": 1})
        self.assertEqual(serialized[0]["step_preview"][0]["step_id"], "s1")
        self.assertFalse(serialized[0]["history_available"])
        self.assertIsNone(serialized[0]["latest_run"])

    async def test_get_workflow_definition_serializes_typed_boundaries(self) -> None:
        service = self._service(
            WorkflowSpec(
                workflow_id="wf",
                input_model=TriggerInput,
                steps=[
                    CodeStep(
                        step_id="double",
                        run=_double,
                        input=TriggerInput,
                        output=StepOutput,
                        timeout_s=10,
                    )
                ],
                metadata={"source": "test"},
            )
        )

        definition = await service.get_workflow_definition("wf")

        self.assertEqual(definition["mode"], "pipeline")
        self.assertEqual(definition["metadata"], {"source": "test"})
        self.assertIn("count", definition["input_schema"]["properties"])
        self.assertEqual(definition["steps"][0]["ordinal"], 0)
        self.assertEqual(definition["steps"][0]["kind"], "code")
        self.assertEqual(definition["steps"][0]["agent_ids"], [])
        self.assertIn("doubled", definition["steps"][0]["output_schema"]["properties"])
        self.assertEqual(definition["steps"][0]["timeout_s"], 10.0)
        self.assertNotIn("strategy", definition)

    async def test_run_workflow_stores_a_queued_run_and_starts_it(self) -> None:
        service, store = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )

        run = await service.run_workflow(
            "wf", dedup_key="manual", workflow_input={"n": 1}, user_id="u-1"
        )

        self.assertEqual(run.status, "queued")
        self.assertTrue(run.run_id.startswith(workflow_run_id_prefix("runner-1", "wf")))
        stored = await store.get_run(run.run_id)
        self.assertEqual(stored.status, "queued")
        self.assertEqual(stored.dedup_key, "manual")
        self.assertEqual(stored.workflow_input, {"n": 1})
        self.assertEqual(stored.user_id, "u-1")
        self.assertEqual([r.run_id for r in self.started], [run.run_id])

    async def test_queued_run_is_readable_and_streamable_at_once(self) -> None:
        service, _ = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )
        run = await service.run_workflow("wf", user_id="u-1")

        fetched = await service.get_run("wf", run.run_id)
        events = await service.stream_run_events("wf", run.run_id)

        self.assertEqual(fetched.status, "queued")
        self.assertTrue(hasattr(events, "__anext__"))

    async def test_run_of_a_workflow_with_agents_requires_a_user(self) -> None:
        service, _ = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )
        with self.assertRaises(WorkflowUserRequiredError):
            await service.run_workflow("wf")
        with self.assertRaises(ValueError):
            await service.run_workflow("wf", user_id="has space")
        self.assertEqual(self.started, [])

    async def test_code_only_workflow_runs_without_a_user(self) -> None:
        service, _ = self._stored_service(
            WorkflowSpec(
                workflow_id="wf",
                steps=[CodeStep(step_id="double", run=_double, input=TriggerInput, output=StepOutput)],
            )
        )
        run = await service.run_workflow("wf", workflow_input={"count": 1})
        self.assertIsNone(run.user_id)

    async def test_run_workflow_rejects_non_object_input(self) -> None:
        service, _ = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )
        with self.assertRaises(ValueError):
            await service.run_workflow("wf", workflow_input=[1, 2, 3])  # type: ignore[arg-type]

    async def test_run_workflow_validates_declared_input_before_storing(self) -> None:
        service, store = self._stored_service(
            WorkflowSpec(
                workflow_id="wf",
                input_model=TriggerInput,
                steps=[
                    CodeStep(
                        step_id="double",
                        run=_double,
                        input=TriggerInput,
                        output=StepOutput,
                    )
                ],
            )
        )
        with self.assertRaises(WorkflowInputValidationError) as raised:
            await service.run_workflow("wf", workflow_input={"count": "bad"})
        self.assertEqual(self.started, [])
        self.assertEqual(await store.list_runs("wf"), [])
        self.assertEqual(raised.exception.workflow_id, "wf")
        self.assertEqual(raised.exception.errors[0]["loc"], ("count",))

    async def test_duplicate_dedup_key_is_rejected_while_the_run_is_active(self) -> None:
        service, store = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )
        first = await service.run_workflow("wf", dedup_key="manual", user_id="u-1")

        with self.assertRaises(DuplicateWorkflowRunError) as raised:
            await service.run_workflow("wf", dedup_key="manual", user_id="u-1")
        self.assertEqual(raised.exception.existing_run.run_id, first.run_id)

        await store.finish_run(first.run_id, status="completed", finished_at=2.0)
        second = await service.run_workflow("wf", dedup_key="manual", user_id="u-1")
        self.assertNotEqual(second.run_id, first.run_id)

    async def test_run_unknown_to_the_store_is_not_found(self) -> None:
        service, _ = self._stored_service(
            WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")])
        )
        with self.assertRaises(WorkflowNotFoundError):
            await service.get_run("wf", "mw:runner-1:wf:missing")
        with self.assertRaises(WorkflowNotFoundError):
            await service.stream_run_events("wf", "mw:runner-1:wf:missing")

    async def test_unknown_workflow_is_rejected(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))
        with self.assertRaises(WorkflowNotFoundError):
            await service.list_runs("missing")


class WorkflowIdHelperTests(unittest.TestCase):
    def test_run_id_carries_prefix(self) -> None:
        prefix = workflow_run_id_prefix("r_abc", "wf")
        run_id = make_run_id("r_abc", "wf")
        self.assertTrue(run_id.startswith(prefix))
        self.assertTrue(prefix.startswith("mw:r_abc:wf:"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Tests for the workflow registry and service (DBOS-mocked, no Postgres)."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

from pydantic import BaseModel

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentPool

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
    WorkflowStrategy,
)
from mash.workflows import dbos as workflow_dbos


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


class _DummyStrategy(WorkflowStrategy):
    async def run(self, ctx: Any) -> dict[str, Any]:  # pragma: no cover - not executed
        return {}


@dataclass
class _FakeWorkflowStatus:
    workflow_id: str
    status: str
    created_at: int = 1_700_000_000_000
    updated_at: int = 1_700_000_001_000
    output: dict[str, Any] | None = None
    error: Exception | None = None
    deduplication_id: str | None = None


class _FakeHost:
    def __init__(self, registry: WorkflowRegistry) -> None:
        self.runtime_database_url = "postgresql://example"
        self._registry = registry

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._registry

    def get_workflow_store(self) -> None:
        return None


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

    def test_strategy_only_workflow_is_valid(self) -> None:
        registry = WorkflowRegistry()
        registry.register(WorkflowSpec(workflow_id="wf", strategy=_DummyStrategy()))
        self.assertEqual([w.workflow_id for w in registry.list()], ["wf"])

    def test_workflow_without_steps_or_strategy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowSpec(workflow_id="wf")


class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, workflow: WorkflowSpec) -> WorkflowService:
        registry = WorkflowRegistry()
        registry.register(workflow)
        return WorkflowService(
            registry, cast("AgentPool", _FakeHost(registry)), runner_id="runner-1"
        )

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
        self.assertFalse(definition["steps"][0]["orchestration"])
        self.assertIn("doubled", definition["steps"][0]["output_schema"]["properties"])
        self.assertEqual(definition["steps"][0]["timeout_s"], 10.0)

    async def test_get_strategy_definition_does_not_invent_steps(self) -> None:
        service = self._service(
            WorkflowSpec(workflow_id="wf", strategy=_DummyStrategy())
        )

        definition = await service.get_workflow_definition("wf")

        self.assertEqual(definition["mode"], "strategy")
        self.assertEqual(definition["strategy"], "_DummyStrategy")
        self.assertEqual(definition["steps"], [])

    async def test_run_workflow_starts_dbos_and_returns_status(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))

        async def start_workflow_run(**kwargs):
            self.assertEqual(kwargs["runner_id"], "runner-1")
            self.assertEqual(kwargs["dedup_key"], "manual")
            return "mw:host-1:wf:abc"

        async def get_workflow_status(run_id):
            return _FakeWorkflowStatus(workflow_id=run_id, status="ENQUEUED")

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run), patch.object(
            workflow_dbos, "get_workflow_status", get_workflow_status
        ):
            run = await service.run_workflow("wf", dedup_key="manual")
        self.assertEqual(run.run_id, "mw:host-1:wf:abc")
        self.assertEqual(run.status, "queued")

    async def test_run_workflow_passes_input(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))
        seen: dict[str, Any] = {}

        async def start_workflow_run(**kwargs):
            seen.update(kwargs)
            return "mw:host-1:wf:abc"

        async def get_workflow_status(_run_id):
            return _FakeWorkflowStatus(workflow_id="mw:host-1:wf:abc", status="ENQUEUED")

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run), patch.object(
            workflow_dbos, "get_workflow_status", get_workflow_status
        ):
            await service.run_workflow("wf", workflow_input={"n": 1})
        self.assertEqual(seen["workflow_input"], {"n": 1})

    async def test_run_workflow_rejects_non_object_input(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))
        with self.assertRaises(ValueError):
            await service.run_workflow("wf", workflow_input=[1, 2, 3])  # type: ignore[arg-type]

    async def test_run_workflow_validates_declared_input_before_enqueue(self) -> None:
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
                    )
                ],
            )
        )
        with patch.object(workflow_dbos, "start_workflow_run") as start:
            with self.assertRaises(WorkflowInputValidationError) as raised:
                await service.run_workflow("wf", workflow_input={"count": "bad"})
        start.assert_not_called()
        self.assertEqual(raised.exception.workflow_id, "wf")
        self.assertEqual(raised.exception.errors[0]["loc"], ("count",))

    async def test_duplicate_dedup_key_is_rejected(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))

        async def start_workflow_run(**_kwargs):
            raise workflow_dbos.WorkflowDeduplicatedError("mw:existing")

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run):
            with self.assertRaises(DuplicateWorkflowRunError):
                await service.run_workflow("wf", dedup_key="manual")

    async def test_get_run_maps_strategy_dbos_status(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", strategy=_DummyStrategy()))

        async def get_workflow_status(run_id):
            return _FakeWorkflowStatus(
                workflow_id=run_id, status="SUCCESS", output={"ok": True}
            )

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            run = await service.get_run("wf", "mw:host-1:wf:abc")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.result, {"ok": True})

    async def test_unknown_workflow_is_rejected(self) -> None:
        service = self._service(WorkflowSpec(workflow_id="wf", steps=[_agent_step("s1", "worker")]))
        with self.assertRaises(WorkflowNotFoundError):
            await service.list_runs("missing")


class WorkflowIdHelperTests(unittest.TestCase):
    def test_run_id_carries_prefix(self) -> None:
        prefix = workflow_dbos.workflow_run_id_prefix("r_abc", "wf")
        run_id = workflow_dbos.make_run_id("r_abc", "wf")
        self.assertTrue(run_id.startswith(prefix))
        self.assertTrue(prefix.startswith("mw:r_abc:wf:"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

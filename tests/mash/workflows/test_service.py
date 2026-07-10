"""Tests for the v2 workflow registry and service (DBOS-mocked, no Postgres)."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from mash.testing.runtime_fixtures import build_spec
from mash.workflows import (
    AgentStep,
    DuplicateWorkflowRunError,
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
        return WorkflowService(registry, _FakeHost(registry), runner_id="runner-1")

    async def test_list_workflows_serializes_steps_and_metadata(self) -> None:
        service = self._service(
            WorkflowSpec(
                workflow_id="wf",
                steps=[_agent_step("s1", "worker")],
                metadata={"source": "test"},
            )
        )
        serialized = await service.list_workflows()
        self.assertEqual(serialized[0]["workflow_id"], "wf")
        self.assertEqual(serialized[0]["steps"][0]["step_id"], "s1")
        self.assertEqual(serialized[0]["steps"][0]["kind"], "agent")
        self.assertEqual(serialized[0]["metadata"], {"source": "test"})

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
        self.assertEqual(run.output, {"ok": True})

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

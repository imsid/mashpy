"""Tests for workflow orchestration service."""

from __future__ import annotations

import json
import inspect
import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import build_spec
from mash.workflows import (
    DuplicateWorkflowRunError,
    TaskSpec,
    WorkflowNotFoundError,
    WorkflowRegistry,
    WorkflowService,
    WorkflowSpec,
)
from mash.workflows import dbos as workflow_dbos
from mash.workflows.service import workflow_task_session_id


def _task(task_id: str, agent_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        agent_spec=build_spec(agent_id=agent_id, response_text="{}"),
    )


@dataclass
class _RequestRecord:
    request_id: str
    payload: dict[str, Any]
    session_id: str


@dataclass
class _FakeWorkflowStatus:
    workflow_id: str
    status: str
    created_at: int = 1_700_000_000_000
    updated_at: int = 1_700_000_001_000
    output: dict[str, Any] | None = None
    error: Exception | None = None
    deduplication_id: str | None = None


class _FakeAgentClient:
    def __init__(self, text: str = "{}", *, event: str = "request.completed") -> None:
        self.agent_id = "fake-agent"
        self.text = text
        self.event = event
        self.requests: list[_RequestRecord] = []

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        timeout: float = 30.0,
    ) -> str:
        del timeout
        request_id = f"req_{len(self.requests) + 1}"
        self.requests.append(
            _RequestRecord(
                request_id=request_id,
                payload=json.loads(message),
                session_id=session_id,
            )
        )
        return request_id

    async def stream_response(
        self,
        request_id: str,
        *,
        timeout: float | None = None,
    ):
        del timeout
        if self.event == "request.error":
            yield {"event": "request.error", "data": {"error": "boom"}}
            return
        yield {
            "event": "request.completed",
            "data": {
                "request_id": request_id,
                "response": {
                    "text": self.text,
                },
            },
        }


class _FakeRuntimeStore:
    def __init__(self, events: list[RuntimeEvent]) -> None:
        self.events = list(events)
        self.calls: list[dict[str, Any]] = []

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        del trace_id, limit
        self.calls.append(
            {
                "app_id": app_id,
                "session_id": session_id,
                "after_event_id": after_event_id,
            }
        )
        return [
            event
            for event in self.events
            if event.app_id == app_id
            and event.session_id == session_id
            and event.event_id > after_event_id
        ]


class _FakeRuntime:
    def __init__(self, runtime_store: _FakeRuntimeStore) -> None:
        self.runtime_store = runtime_store


class _FakeHost:
    def __init__(
        self,
        registry: WorkflowRegistry,
        clients: dict[str, _FakeAgentClient],
        agents: dict[str, _FakeRuntime] | None = None,
    ) -> None:
        self.runtime_database_url = "postgresql://example"
        self._registry = registry
        self._clients = dict(clients)
        self._agents = dict(agents or {})

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._registry

    def get_client(self, agent_id: str) -> _FakeAgentClient:
        try:
            return self._clients[agent_id]
        except KeyError as exc:
            raise ValueError(f"agent client '{agent_id}' is not registered") from exc

    def get_agent(self, agent_id: str) -> _FakeRuntime:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"agent '{agent_id}' is not registered") from exc


class _FakeDBOS:
    statuses: list[_FakeWorkflowStatus] = []

    @staticmethod
    async def run_step_async(_config, func, *args, **kwargs):
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    @classmethod
    async def list_workflows_async(cls, **_kwargs):
        return list(cls.statuses)


class WorkflowRegistryTests(unittest.TestCase):
    def test_register_get_and_list(self) -> None:
        registry = WorkflowRegistry()
        workflow = WorkflowSpec(
            workflow_id="changelog",
            tasks=[_task("scan", "worker")],
        )
        registry.register(workflow)
        self.assertIs(registry.get("changelog"), workflow)
        self.assertEqual(registry.list(), [workflow])

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = WorkflowRegistry()
        workflow = WorkflowSpec(
            workflow_id="changelog",
            tasks=[_task("scan", "worker")],
        )
        registry.register(workflow)
        with self.assertRaises(ValueError):
            registry.register(workflow)


class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_workflows_serializes_specs(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="changelog",
                tasks=[_task("scan", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")
        workflows = await service.list_workflows()
        self.assertEqual(
            workflows,
            [
                {
                    "workflow_id": "changelog",
                    "tasks": [{"task_id": "scan", "agent_id": "worker"}],
                }
            ],
        )

    async def test_list_runs_uses_dbos_prefix_filters_and_maps_statuses(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")
        calls: list[dict[str, Any]] = []

        async def list_workflow_statuses(**kwargs):
            calls.append(kwargs)
            return [
                _FakeWorkflowStatus(
                    workflow_id="mw:host-1:wf:abc",
                    status="SUCCESS",
                    deduplication_id="wf:manual",
                )
            ]

        with patch.object(workflow_dbos, "list_workflow_statuses", list_workflow_statuses):
            runs = await service.list_runs(
                "wf",
                status="queued",
                start_time="2026-05-01T00:00:00Z",
                end_time="2026-05-20T00:00:00Z",
                limit=25,
                offset=5,
                sort_desc=False,
            )

        self.assertEqual(
            calls,
            [
                {
                    "host_id": "host-1",
                    "workflow_id": "wf",
                    "status": ["PENDING", "ENQUEUED", "DELAYED"],
                    "start_time": "2026-05-01T00:00:00Z",
                    "end_time": "2026-05-20T00:00:00Z",
                    "limit": 25,
                    "offset": 5,
                    "sort_desc": False,
                }
            ],
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run_id, "mw:host-1:wf:abc")
        self.assertEqual(runs[0].workflow_id, "wf")
        self.assertEqual(runs[0].dedup_key, "manual")
        self.assertEqual(runs[0].status, "completed")
        self.assertIsNone(runs[0].output)

    async def test_list_runs_unknown_workflow_raises_not_found(self) -> None:
        registry = WorkflowRegistry()
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")

        with self.assertRaises(WorkflowNotFoundError):
            await service.list_runs("missing")

    async def test_dbos_list_workflow_statuses_uses_lightweight_listing(self) -> None:
        calls: list[dict[str, Any]] = []

        class _ListDBOS:
            @staticmethod
            async def list_workflows_async(**kwargs):
                calls.append(kwargs)
                return [_FakeWorkflowStatus(workflow_id="mw:host-1:wf:abc", status="SUCCESS")]

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_ListDBOS, None, None, None, None),
        ):
            statuses = await workflow_dbos.list_workflow_statuses(
                host_id="host-1",
                workflow_id="wf",
                status="SUCCESS",
                start_time="2026-05-01T00:00:00Z",
                end_time="2026-05-20T00:00:00Z",
                limit=25,
                offset=5,
                sort_desc=False,
            )

        self.assertEqual(len(statuses), 1)
        self.assertEqual(
            calls,
            [
                {
                    "name": "mash.workflow.execute",
                    "workflow_id_prefix": "mw:host-1:wf:",
                    "status": "SUCCESS",
                    "start_time": "2026-05-01T00:00:00Z",
                    "end_time": "2026-05-20T00:00:00Z",
                    "limit": 25,
                    "offset": 5,
                    "sort_desc": False,
                    "load_input": False,
                    "load_output": False,
                }
            ],
        )

    async def test_run_workflow_starts_dbos_workflow_and_returns_status(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        host = _FakeHost(registry, {})
        service = WorkflowService(registry, host, host_id="host-1")
        status = _FakeWorkflowStatus(
            workflow_id="mw:host-1:wf:abc",
            status="ENQUEUED",
            deduplication_id="wf:manual",
        )

        async def start_workflow_run(**kwargs):
            self.assertEqual(kwargs["database_url"], "postgresql://example")
            self.assertEqual(kwargs["host_id"], "host-1")
            self.assertEqual(kwargs["workflow"].workflow_id, "wf")
            self.assertEqual(kwargs["dedup_key"], "manual")
            self.assertEqual(kwargs["workflow_input"], {})
            return "mw:host-1:wf:abc"

        async def get_workflow_status(run_id):
            self.assertEqual(run_id, "mw:host-1:wf:abc")
            return status

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run), patch.object(
            workflow_dbos,
            "get_workflow_status",
            get_workflow_status,
        ):
            run = await service.run_workflow("wf", dedup_key="manual")

        self.assertEqual(run.run_id, "mw:host-1:wf:abc")
        self.assertEqual(run.workflow_id, "wf")
        self.assertEqual(run.dedup_key, "manual")
        self.assertEqual(run.status, "queued")

    async def test_run_workflow_passes_workflow_input(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")
        workflow_input = {"target_agent_id": "primary"}

        async def start_workflow_run(**kwargs):
            self.assertEqual(kwargs["workflow_input"], workflow_input)
            return "mw:host-1:wf:abc"

        async def get_workflow_status(_run_id):
            return _FakeWorkflowStatus(
                workflow_id="mw:host-1:wf:abc",
                status="ENQUEUED",
            )

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run), patch.object(
            workflow_dbos,
            "get_workflow_status",
            get_workflow_status,
        ):
            await service.run_workflow("wf", workflow_input=workflow_input)

    async def test_run_workflow_rejects_non_object_workflow_input(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")

        with self.assertRaises(ValueError):
            await service.run_workflow("wf", workflow_input=["bad"])  # type: ignore[arg-type]

    async def test_duplicate_active_dedup_key_is_rejected(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")

        async def start_workflow_run(**_kwargs):
            raise workflow_dbos.WorkflowDeduplicatedError("mw:host-1:wf:old")

        with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run):
            with self.assertRaises(DuplicateWorkflowRunError) as raised:
                await service.run_workflow("wf", dedup_key="manual")
        self.assertEqual(raised.exception.existing_run.run_id, "mw:host-1:wf:old")

    async def test_get_run_maps_dbos_status(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")

        async def get_workflow_status(run_id):
            self.assertEqual(run_id, "mw:host-1:wf:abc")
            return _FakeWorkflowStatus(
                workflow_id=run_id,
                status="SUCCESS",
                output={"task_states": {"task-1": {"ok": True}}},
                deduplication_id=None,
            )

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            run = await service.get_run("wf", "mw:host-1:wf:abc")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.finished_at, 1_700_000_001.0)
        self.assertEqual(run.output, {"task_states": {"task-1": {"ok": True}}})

    async def test_stream_run_events_uses_deterministic_task_session_and_cursor(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        run_id = "mw:host-1:wf:abc"
        session_id = workflow_task_session_id(
            workflow_id="wf",
            task_id="task-1",
            run_id=run_id,
        )
        store = _FakeRuntimeStore(
            [
                RuntimeEvent(
                    event_id=1,
                    request_id="req-1",
                    app_id="worker",
                    agent_id="worker",
                    session_id=session_id,
                    event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                ),
                RuntimeEvent(
                    event_id=2,
                    request_id="req-1",
                    app_id="worker",
                    agent_id="worker",
                    session_id=session_id,
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    payload={"request_id": "req-1", "response": {"text": "{}"}},
                ),
            ]
        )
        service = WorkflowService(
            registry,
            _FakeHost(registry, {}, agents={"worker": _FakeRuntime(store)}),
            host_id="host-1",
        )
        statuses = [
            _FakeWorkflowStatus(workflow_id=run_id, status="RUNNING"),
            _FakeWorkflowStatus(workflow_id=run_id, status="SUCCESS"),
        ]

        async def get_workflow_status(_run_id):
            return statuses.pop(0) if statuses else _FakeWorkflowStatus(
                workflow_id=run_id,
                status="SUCCESS",
            )

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            events = [event async for event in stream]

        self.assertEqual(
            [event.event for event in events],
            [
                "workflow.status",
                "workflow.task.started",
                "request.accepted",
                "request.completed",
                "workflow.task.completed",
                "workflow.status",
            ],
        )
        self.assertEqual(store.calls[0]["session_id"], session_id)
        self.assertEqual(store.calls[0]["after_event_id"], 0)
        self.assertEqual(store.calls[1]["after_event_id"], 2)
        self.assertEqual(events[2].data["workflow_id"], "wf")
        self.assertEqual(events[2].data["task_id"], "task-1")
        self.assertEqual(events[2].data["task_agent_id"], "worker")

    async def test_stream_run_events_flushes_task_events_for_terminal_run(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        run_id = "mw:host-1:wf:abc"
        session_id = workflow_task_session_id(
            workflow_id="wf",
            task_id="task-1",
            run_id=run_id,
        )
        store = _FakeRuntimeStore(
            [
                RuntimeEvent(
                    event_id=7,
                    request_id="req-1",
                    app_id="worker",
                    agent_id="worker",
                    session_id=session_id,
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    payload={"request_id": "req-1", "response": {"text": "{}"}},
                )
            ]
        )
        service = WorkflowService(
            registry,
            _FakeHost(registry, {}, agents={"worker": _FakeRuntime(store)}),
            host_id="host-1",
        )

        async def get_workflow_status(_run_id):
            return _FakeWorkflowStatus(workflow_id=run_id, status="SUCCESS")

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            events = [event async for event in stream]

        self.assertIn("request.completed", [event.event for event in events])
        self.assertEqual(events[-1].event, "workflow.task.completed")

    async def test_unknown_workflow_is_rejected(self) -> None:
        registry = WorkflowRegistry()
        service = WorkflowService(registry, _FakeHost(registry, {}), host_id="host-1")
        with self.assertRaises(WorkflowNotFoundError):
            await service.run_workflow("missing")


class WorkflowDBOSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _FakeDBOS.statuses = []

    async def asyncTearDown(self) -> None:
        workflow_dbos.unregister_host("host-1", getattr(self, "host", None))

    async def test_compact_id_helpers_preserve_prefix_semantics(self) -> None:
        host_id = workflow_dbos.make_host_id()
        run_id = workflow_dbos.make_run_id(host_id, "wf")

        self.assertRegex(host_id, r"^h_[A-Za-z0-9_-]{12}$")
        self.assertTrue(run_id.startswith(f"mw:{host_id}:wf:"))
        legacy_run_id_length = 14 + len(host_id) + len(":wf:") + 32
        self.assertLess(len(run_id), legacy_run_id_length)
        self.assertEqual(
            workflow_dbos.workflow_run_id_prefix(host_id, "wf"),
            f"mw:{host_id}:wf:",
        )

    async def test_execute_workflow_passes_previous_task_state(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        client = _FakeAgentClient(text=json.dumps({"count": 2}))
        self.host = _FakeHost(registry, {"worker": client})
        workflow_dbos.register_host("host-1", self.host)
        _FakeDBOS.statuses = [
            _FakeWorkflowStatus(
                workflow_id="mw:host-1:wf:old",
                status="SUCCESS",
                output={"task_states": {"task-1": {"count": 1}}},
            )
        ]

        with patch.object(workflow_dbos, "_load_dbos_api", return_value=(_FakeDBOS, None, None, None, None)):
            output = await workflow_dbos.execute_registered_workflow(
                "host-1",
                "wf",
                "mw:host-1:wf:new",
                workflow_input={"target_agent_id": "primary"},
            )

        self.assertEqual(client.requests[0].payload["task_state"], {"count": 1})
        self.assertEqual(
            client.requests[0].payload["workflow_input"],
            {"target_agent_id": "primary"},
        )
        self.assertEqual(output["task_states"]["task-1"], {"count": 2})

    async def test_execute_workflow_passes_same_input_to_multiple_tasks(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[
                    _task("task-1", "worker-1"),
                    _task("task-2", "worker-2"),
                ],
            )
        )
        clients = {
            "worker-1": _FakeAgentClient(text=json.dumps({"one": True})),
            "worker-2": _FakeAgentClient(text=json.dumps({"two": True})),
        }
        self.host = _FakeHost(registry, clients)
        workflow_dbos.register_host("host-1", self.host)
        workflow_input = {"target_agent_id": "primary"}

        with patch.object(workflow_dbos, "_load_dbos_api", return_value=(_FakeDBOS, None, None, None, None)):
            await workflow_dbos.execute_registered_workflow(
                "host-1",
                "wf",
                "mw:host-1:wf:new",
                workflow_input=workflow_input,
            )

        self.assertEqual(clients["worker-1"].requests[0].payload["workflow_input"], workflow_input)
        self.assertEqual(clients["worker-2"].requests[0].payload["workflow_input"], workflow_input)

    async def test_execute_workflow_rejects_invalid_task_json(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        self.host = _FakeHost(registry, {"worker": _FakeAgentClient(text="not-json")})
        workflow_dbos.register_host("host-1", self.host)

        with patch.object(workflow_dbos, "_load_dbos_api", return_value=(_FakeDBOS, None, None, None, None)):
            with self.assertRaises(RuntimeError):
                await workflow_dbos.execute_registered_workflow(
                    "host-1",
                    "wf",
                    "mw:host-1:wf:new",
                )

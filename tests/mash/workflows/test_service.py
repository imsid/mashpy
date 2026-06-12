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
    WorkflowTaskMessageSpec,
)
from mash.workflows import dbos as workflow_dbos
from mash.workflows.service import (
    parse_workflow_task_session_id,
    workflow_task_session_id,
)


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
    structured_output: dict[str, Any] | None = None


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
        structured_output: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> str:
        del timeout
        request_id = f"req_{len(self.requests) + 1}"
        self.requests.append(
            _RequestRecord(
                request_id=request_id,
                payload=json.loads(message),
                session_id=session_id,
                structured_output=structured_output,
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
        response: dict[str, Any] = {
            "text": self.text,
        }
        try:
            decoded = json.loads(self.text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            response["structured_output"] = decoded
        elif decoded is not None:
            response["structured_output"] = decoded
        yield {
            "event": "request.completed",
            "data": {
                "request_id": request_id,
                "response": response,
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


class _FakeMemoryStore:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def list_workflow_turns(
        self,
        app_id: str,
        session_prefix: str,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "app_id": app_id,
                "session_prefix": session_prefix,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "offset": offset,
                "sort_desc": sort_desc,
            }
        )
        rows = [
            turn
            for turn in self.turns
            if turn["session_id"].startswith(session_prefix)
            and turn.get("app_id", app_id) == app_id
        ]
        if start_time is not None:
            rows = [turn for turn in rows if float(turn["created_at"]) >= start_time]
        if end_time is not None:
            rows = [turn for turn in rows if float(turn["created_at"]) <= end_time]
        rows = sorted(
            rows,
            key=lambda turn: (float(turn["created_at"]), str(turn["turn_id"])),
            reverse=sort_desc,
        )
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows]


class _FakeRuntime:
    def __init__(
        self,
        runtime_store: _FakeRuntimeStore | None = None,
        memory_store: _FakeMemoryStore | None = None,
    ) -> None:
        self.memory_store = memory_store or _FakeMemoryStore([])
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

    def test_upsert_replaces_existing_workflow(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="changelog",
                tasks=[_task("scan", "worker-a")],
            )
        )
        replacement = WorkflowSpec(
            workflow_id="changelog",
            tasks=[_task("summarize", "worker-b")],
            metadata={"source": "crew"},
        )

        registry.upsert(replacement)

        self.assertIs(registry.get("changelog"), replacement)
        self.assertEqual(registry.list(), [replacement])

    def test_unregister_is_idempotent(self) -> None:
        registry = WorkflowRegistry()
        workflow = WorkflowSpec(
            workflow_id="changelog",
            tasks=[_task("scan", "worker")],
        )
        registry.register(workflow)

        registry.unregister("changelog")
        registry.unregister("changelog")

        self.assertEqual(registry.list(), [])

    def test_invalid_workflow_shape_is_rejected(self) -> None:
        registry = WorkflowRegistry()

        with self.assertRaises(ValueError):
            registry.register(
                WorkflowSpec(workflow_id="", tasks=[_task("scan", "worker")])
            )
        with self.assertRaises(ValueError):
            registry.register(WorkflowSpec(workflow_id="wf", tasks=[]))
        with self.assertRaises(ValueError):
            registry.register(
                WorkflowSpec(
                    workflow_id="wf",
                    tasks=[_task("scan", "worker"), _task("scan", "worker")],
                )
            )


class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_workflows_serializes_specs(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="changelog",
                tasks=[_task("scan", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")
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

    async def test_list_workflows_includes_metadata_when_present(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="changelog",
                tasks=[_task("scan", "worker")],
                metadata={"source": "crew", "version": 1},
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

        workflows = await service.list_workflows()

        self.assertEqual(workflows[0]["metadata"], {"source": "crew", "version": 1})

    async def test_list_runs_uses_memory_turns_and_maps_summaries(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        run_id = "mw:h_TI1UUyBX5w8Q:wf:bHfMwMfMsPDPHI60"
        session_id = workflow_task_session_id(
            workflow_id="wf",
            task_id="task-1",
            run_id=run_id,
        )
        memory_store = _FakeMemoryStore(
            [
                {
                    "turn_id": "turn-1",
                    "session_id": session_id,
                    "app_id": "worker",
                    "user_message": "run input",
                    "agent_response": '{"ok":true}',
                    "metadata": {},
                    "created_at": 1_700_000_000.0,
                }
            ]
        )
        service = WorkflowService(
            registry,
            _FakeHost(
                registry,
                {},
                agents={"worker": _FakeRuntime(memory_store=memory_store)},
            ),
            runner_id="runner-1",
        )

        self.assertFalse(hasattr(workflow_dbos, "list_workflow_statuses"))
        runs = await service.list_runs("wf")

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].run_id, run_id)
        self.assertEqual(runs[0].workflow_id, "wf")
        self.assertIsNone(runs[0].dedup_key)
        self.assertEqual(runs[0].status, "completed")
        self.assertEqual(runs[0].summary["turn_id"], "turn-1")
        self.assertEqual(runs[0].summary["session_id"], session_id)
        self.assertEqual(runs[0].summary["task_id"], "task-1")
        self.assertEqual(runs[0].summary["agent_id"], "worker")
        self.assertEqual(runs[0].summary["user_message"], "run input")
        self.assertEqual(runs[0].summary["agent_response"], '{"ok":true}')
        self.assertEqual(
            memory_store.calls,
            [
                {
                    "app_id": "worker",
                    "session_prefix": "workflow:wf:task:task-1:run:",
                    "start_time": None,
                    "end_time": None,
                    "limit": None,
                    "offset": 0,
                    "sort_desc": True,
                }
            ],
        )

    async def test_list_runs_returns_empty_for_non_completed_status(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

        runs = await service.list_runs("wf", status="failed")

        self.assertEqual(runs, [])

    def test_parse_workflow_task_session_id_preserves_colon_run_id(self) -> None:
        session_id = (
            "workflow:masher-trace-digest:task:digest-traces:run:"
            "mw:h_TI1UUyBX5w8Q:masher-trace-digest:bHfMwMfMsPDPHI60"
        )

        parsed = parse_workflow_task_session_id(session_id)

        self.assertEqual(
            parsed,
            (
                "masher-trace-digest",
                "digest-traces",
                "mw:h_TI1UUyBX5w8Q:masher-trace-digest:bHfMwMfMsPDPHI60",
            ),
        )

    async def test_list_runs_unknown_workflow_raises_not_found(self) -> None:
        registry = WorkflowRegistry()
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

        with self.assertRaises(WorkflowNotFoundError):
            await service.list_runs("missing")

    async def test_run_workflow_starts_dbos_workflow_and_returns_status(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        host = _FakeHost(registry, {})
        service = WorkflowService(registry, host, runner_id="runner-1")
        status = _FakeWorkflowStatus(
            workflow_id="mw:host-1:wf:abc",
            status="ENQUEUED",
            deduplication_id="wf:manual",
        )

        async def start_workflow_run(**kwargs):
            self.assertEqual(kwargs["database_url"], "postgresql://example")
            self.assertEqual(kwargs["runner_id"], "runner-1")
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
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")
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
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

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
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

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
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")

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
            runner_id="runner-1",
        )
        async def get_workflow_status(run_id_value):
            self.assertEqual(run_id_value, run_id)
            return _FakeWorkflowStatus(workflow_id=run_id, status="SUCCESS")

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            events = [event async for event in stream]

        self.assertEqual(
            [event.event for event in events],
            [
                "workflow.task.started",
                "request.accepted",
                "request.completed",
                "workflow.task.completed",
            ],
        )
        self.assertEqual(store.calls[0]["session_id"], session_id)
        self.assertEqual(store.calls[0]["after_event_id"], 0)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(events[1].data["workflow_id"], "wf")
        self.assertEqual(events[1].data["task_id"], "task-1")
        self.assertEqual(events[1].data["task_agent_id"], "worker")

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
            runner_id="runner-1",
        )

        async def get_workflow_status(run_id_value):
            self.assertEqual(run_id_value, run_id)
            return _FakeWorkflowStatus(workflow_id=run_id, status="SUCCESS")

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            events = [event async for event in stream]

        self.assertIn("request.completed", [event.event for event in events])
        self.assertEqual(events[-1].event, "workflow.task.completed")

    async def test_stream_run_events_waits_when_run_exists_before_task_events(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        run_id = "mw:host-1:wf:abc"
        store = _FakeRuntimeStore([])
        service = WorkflowService(
            registry,
            _FakeHost(registry, {}, agents={"worker": _FakeRuntime(store)}),
            runner_id="runner-1",
        )

        async def get_workflow_status(run_id_value):
            self.assertEqual(run_id_value, run_id)
            return _FakeWorkflowStatus(workflow_id=run_id, status="ENQUEUED")

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            event = await anext(stream)
            await stream.aclose()

        self.assertEqual(event.comment, "keep-alive")

    async def test_stream_run_events_surfaces_dbos_error_after_task_response(self) -> None:
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
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    payload={"request_id": "req-1", "response": {"text": "not-json"}},
                )
            ]
        )
        service = WorkflowService(
            registry,
            _FakeHost(registry, {}, agents={"worker": _FakeRuntime(store)}),
            runner_id="runner-1",
        )

        async def get_workflow_status(run_id_value):
            self.assertEqual(run_id_value, run_id)
            return _FakeWorkflowStatus(
                workflow_id=run_id,
                status="ERROR",
                error=RuntimeError("workflow task output must be valid JSON"),
            )

        with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
            stream = await service.stream_run_events("wf", run_id, poll_interval=0)
            events = [event async for event in stream]

        self.assertEqual(events[-1].event, "workflow.error")
        self.assertIn("valid JSON", events[-1].data["error"])

    async def test_unknown_workflow_is_rejected(self) -> None:
        registry = WorkflowRegistry()
        service = WorkflowService(registry, _FakeHost(registry, {}), runner_id="runner-1")
        with self.assertRaises(WorkflowNotFoundError):
            await service.run_workflow("missing")


class WorkflowDBOSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _FakeDBOS.statuses = []

    async def asyncTearDown(self) -> None:
        workflow_dbos.unregister_runner("runner-1", getattr(self, "host", None))

    async def test_compact_id_helpers_preserve_prefix_semantics(self) -> None:
        runner_id = workflow_dbos.make_runner_id()
        run_id = workflow_dbos.make_run_id(runner_id, "wf")

        self.assertRegex(runner_id, r"^r_[A-Za-z0-9_-]{12}$")
        self.assertTrue(run_id.startswith(f"mw:{runner_id}:wf:"))
        legacy_run_id_length = 14 + len(runner_id) + len(":wf:") + 32
        self.assertLess(len(run_id), legacy_run_id_length)
        self.assertEqual(
            workflow_dbos.workflow_run_id_prefix(runner_id, "wf"),
            f"mw:{runner_id}:wf:",
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
        workflow_dbos.register_runner("runner-1", self.host)
        _FakeDBOS.statuses = [
            _FakeWorkflowStatus(
                workflow_id="mw:host-1:wf:old",
                status="SUCCESS",
                output={"task_states": {"task-1": {"count": 1}}},
            )
        ]

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            output = await workflow_dbos.execute_registered_workflow(
                "runner-1",
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
        workflow_dbos.register_runner("runner-1", self.host)
        workflow_input = {"target_agent_id": "primary"}

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            await workflow_dbos.execute_registered_workflow(
                "runner-1",
                "wf",
                "mw:host-1:wf:new",
                workflow_input=workflow_input,
            )

        self.assertEqual(
            clients["worker-1"].requests[0].payload["workflow_input"],
            workflow_input,
        )
        self.assertEqual(
            clients["worker-2"].requests[0].payload["workflow_input"],
            workflow_input,
        )

    async def test_execute_dynamic_workflow_adds_skill_task_instructions(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[TaskSpec(task_id="task-1", agent_id="worker")],
                task_message=WorkflowTaskMessageSpec(
                    skill_name="workflow:wf:v1",
                ),
            )
        )
        client = _FakeAgentClient(text=json.dumps({"ok": True}))
        self.host = _FakeHost(registry, {"worker": client})
        workflow_dbos.register_runner("runner-1", self.host)

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            await workflow_dbos.execute_registered_workflow(
                "runner-1",
                "wf",
                "mw:host-1:wf:new",
            )

        payload = client.requests[0].payload
        self.assertEqual(payload["skill_name"], "workflow:wf:v1")
        self.assertNotIn("instruction", payload)
        self.assertIn("workflow_id", payload)
        self.assertIn("task_state", payload)
        self.assertIn(
            "Your first action must be calling the Skill tool",
            payload["workflow_task_instructions"][0],
        )
        self.assertEqual(
            client.requests[0].structured_output,
            {
                "title": "WorkflowTaskState",
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        self.assertNotIn("mandatory_first_action", payload)
        self.assertNotIn("final_response_contract", payload)
        self.assertNotIn("final_response_contract_text", payload)
        self.assertIn(
            "After the Skill tool returns",
            payload["workflow_task_instructions"][1],
        )

    async def test_execute_dynamic_workflow_closes_nested_structured_output_objects(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[
                    TaskSpec(
                        task_id="task-1",
                        agent_id="worker",
                        structured_output={
                            "type": "object",
                            "properties": {
                                "result": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean"},
                                    },
                                    "required": ["ok"],
                                }
                            },
                            "required": ["result"],
                        },
                    )
                ],
            )
        )
        client = _FakeAgentClient(text='{"result":{"ok":true}}')
        self.host = _FakeHost(registry, {"worker": client})
        workflow_dbos.register_runner("runner-1", self.host)

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            await workflow_dbos.execute_registered_workflow(
                "runner-1",
                "wf",
                "mw:host-1:wf:new",
            )

        self.assertEqual(
            client.requests[0].structured_output,
            {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                        },
                        "required": ["ok"],
                        "additionalProperties": False,
                    }
                },
                "required": ["result"],
                "additionalProperties": False,
            },
        )

    async def test_execute_workflow_rejects_invalid_task_json(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        self.host = _FakeHost(registry, {"worker": _FakeAgentClient(text="not-json")})
        workflow_dbos.register_runner("runner-1", self.host)

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            with self.assertRaises(RuntimeError):
                await workflow_dbos.execute_registered_workflow(
                    "runner-1",
                    "wf",
                    "mw:host-1:wf:new",
                )

    async def test_execute_workflow_rejects_non_object_task_json(self) -> None:
        registry = WorkflowRegistry()
        registry.register(
            WorkflowSpec(
                workflow_id="wf",
                tasks=[_task("task-1", "worker")],
            )
        )
        self.host = _FakeHost(registry, {"worker": _FakeAgentClient(text="[]")})
        workflow_dbos.register_runner("runner-1", self.host)

        with patch.object(
            workflow_dbos,
            "_load_dbos_api",
            return_value=(_FakeDBOS, None, None, None, None),
        ):
            with self.assertRaises(RuntimeError):
                await workflow_dbos.execute_registered_workflow(
                    "runner-1",
                    "wf",
                    "mw:host-1:wf:new",
                )

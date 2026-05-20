"""Integration tests for host-managed runtime server contracts."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

from mash.runtime.client import AgentClientError
from mash.runtime.engine.dbos import register_runtime, require_runtime, unregister_runtime
from mash.runtime.engine.workflow import execute_request_workflow
from mash.runtime import HostBuilder
from mash.testing.runtime_fixtures import (
    build_delegating_spec,
    build_spec,
    metadata,
)
from mash.workflows import TaskSpec, WorkflowSpec
from mash.workflows import dbos as workflow_dbos
from mash.tools.subagent import derive_subagent_session_id

_IN_FAKE_DBOS_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_fake_dbos_step",
    default=False,
)


class _FakeWorkflowDBOS:
    @staticmethod
    async def run_step_async(_config, func, *args, **kwargs):
        token = _IN_FAKE_DBOS_STEP.set(True)
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            _IN_FAKE_DBOS_STEP.reset(token)

    @staticmethod
    async def list_workflows_async(**_kwargs):
        return []


class _FailureInspectingWorkflowDBOS:
    fail_payload: dict[str, object] | None = None

    @classmethod
    async def run_step_async(cls, config, func, *args, **kwargs):
        if dict(config or {}).get("name") == "request.fail":
            payload = args[-1]
            if isinstance(payload, BaseException):
                raise AssertionError("request.fail received a raw exception")
            if not isinstance(payload, dict):
                raise AssertionError("request.fail payload must be a dict")
            cls.fail_payload = dict(payload)
        token = _IN_FAKE_DBOS_STEP.set(True)
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            _IN_FAKE_DBOS_STEP.reset(token)

    @staticmethod
    async def list_workflows_async(**_kwargs):
        return []


class _StepRestrictedRequestEngine:
    def __init__(self, runtime, *, database_url: str) -> None:
        self._runtime = runtime
        self._database_url = database_url
        self._tasks: set[asyncio.Task[None]] = set()

    async def open(self) -> None:
        register_runtime(self._runtime)
        return None

    async def close(self) -> None:
        if self._tasks:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
        unregister_runtime(self._runtime)

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, object],
    ) -> None:
        del self._database_url
        if _IN_FAKE_DBOS_STEP.get():
            raise AssertionError()
        task = asyncio.create_task(
            execute_request_workflow(
                self._runtime.app_id,
                request_id,
                message,
                session_id,
                dict(request_metadata or {}),
                require_runtime=require_runtime,
            ),
            name=f"StepRestrictedRequest-{self._runtime.app_id}-{request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class AgentHostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_host_builder_requires_primary(self) -> None:
        with self.assertRaises(ValueError):
            HostBuilder().build()

    def test_host_builder_composes_primary_and_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                host = (
                    HostBuilder()
                    .primary(build_spec(agent_id="primary", response_text="primary-ok"))
                    .subagent(
                        build_spec(
                            agent_id="research",
                            response_text="research-ok",
                        ),
                        metadata=metadata(),
                    )
                    .build()
                )
                described = {item["agent_id"]: item for item in host.describe_agents()}
                self.assertEqual(described["primary"]["role"], "primary")
                self.assertEqual(described["research"]["role"], "subagent")

    def test_host_builder_registers_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_spec(agent_id="primary", response_text="primary-ok")
                host = (
                    HostBuilder()
                    .primary(primary_spec)
                    .workflow(
                        WorkflowSpec(
                            workflow_id="changelog",
                            tasks=[
                                TaskSpec(
                                    task_id="scan",
                                    agent_spec=primary_spec,
                                )
                            ],
                        )
                    )
                    .build()
                )
                self.assertEqual(
                    [item.workflow_id for item in host.get_workflow_registry().list()],
                    ["changelog"],
                )

    async def test_host_builder_registers_multiple_workflow_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                worker_a = build_spec(agent_id="worker-a", response_text="{}")
                worker_b = build_spec(agent_id="worker-b", response_text="{}")
                host = (
                    HostBuilder()
                    .primary(build_spec(agent_id="primary", response_text="primary-ok"))
                    .workflow(
                        WorkflowSpec(
                            workflow_id="wf-a",
                            tasks=[TaskSpec(task_id="task-a", agent_spec=worker_a)],
                        )
                    )
                    .workflow(
                        WorkflowSpec(
                            workflow_id="wf-b",
                            tasks=[TaskSpec(task_id="task-b", agent_spec=worker_b)],
                        )
                    )
                    .build()
                )

                described = {item["agent_id"]: item for item in host.describe_agents()}
                self.assertEqual(sorted(described.keys()), ["primary"])
                self.assertEqual(host.list_agents(), ["primary"])
                self.assertEqual(
                    sorted(item.workflow_id for item in host.get_workflow_registry().list()),
                    ["wf-a", "wf-b"],
                )
                host.configure_runtime_database_url("postgresql://test/runtime")
                await host.start()
                try:
                    self.assertIsNotNone(host.get_client("worker-a"))
                    self.assertIsNotNone(host.get_client("worker-b"))
                finally:
                    await host.close()

    async def test_host_starts_runtime_servers_and_client_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                host = HostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                host.configure_runtime_database_url("postgresql://test/runtime")
                await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")

                    primary = host.get_agent("primary")
                    sessions = await primary.list_sessions()
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["session_id"], "s-1")

                    definitions = primary.get_signal_definitions()
                    self.assertEqual(set(definitions.keys()), {"unused_tools", "unused_tool_tokens"})
                    self.assertEqual(definitions["unused_tools"]["value_type"], "string_list")
                    self.assertEqual(definitions["unused_tool_tokens"]["computed_at"], "turn_complete")

                    signal_rows = await primary.get_session_signals("s-1")
                    self.assertGreaterEqual(len(signal_rows), 1)
                    self.assertEqual(signal_rows[-1]["turn_id"], result["turn_id"])
                    self.assertIn("unused_tools", signal_rows[-1]["signals"])
                    self.assertIn("unused_tool_tokens", signal_rows[-1]["signals"])
                finally:
                    await host.close()

    async def test_host_start_does_not_self_probe_runtime_health_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                host = HostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                with patch(
                    "mash.runtime.client.AgentClient.health",
                    side_effect=AgentClientError("unexpected health probe"),
                ):
                    host.configure_runtime_database_url("postgresql://test/runtime")
                    await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")
                finally:
                    await host.close()

    async def test_host_exposes_workflow_service_for_registered_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_spec(
                    agent_id="primary",
                    response_text='{"last_run_ts":"2026-05-14T00:00:00Z"}',
                )
                host = (
                    HostBuilder()
                    .primary(primary_spec)
                    .workflow(
                        WorkflowSpec(
                            workflow_id="changelog",
                            tasks=[
                                TaskSpec(
                                    task_id="scan-codebase-and-append-changelog",
                                    agent_spec=primary_spec,
                                )
                            ],
                        )
                    )
                    .build()
                )
                host.configure_runtime_database_url("postgresql://test/runtime")
                await host.start()
                try:
                    workflow_service = host.get_workflow_service()
                    self.assertIsNotNone(workflow_service)
                    listed = await workflow_service.list_workflows()
                    self.assertEqual(len(listed), 1)

                    async def start_workflow_run(**_kwargs):
                        return f"mw:{host.host_id}:changelog:abc"

                    async def get_workflow_status(_run_id):
                        return None

                    with patch.object(
                        workflow_dbos,
                        "start_workflow_run",
                        start_workflow_run,
                    ), patch.object(
                        workflow_dbos,
                        "get_workflow_status",
                        get_workflow_status,
                    ):
                        run = await workflow_service.run_workflow("changelog")
                    self.assertEqual(run.status, "queued")
                finally:
                    await host.close()

    async def test_workflow_task_request_runs_inline_from_host_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch("mash.runtime.engine.workflow.DBOS", _FakeWorkflowDBOS):
                    worker_spec = build_spec(
                        agent_id="worker",
                        response_text='{"ok":true}',
                    )
                    host = (
                        HostBuilder()
                        .primary(build_spec(agent_id="primary", response_text="primary-ok"))
                        .workflow(
                            WorkflowSpec(
                                workflow_id="wf",
                                tasks=[TaskSpec(task_id="task", agent_spec=worker_spec)],
                            )
                        )
                        .build()
                    )
                    host.configure_runtime_database_url("postgresql://test/runtime")
                    await host.start()
                    try:
                        with patch.object(
                            workflow_dbos,
                            "_load_dbos_api",
                            return_value=(_FakeWorkflowDBOS, None, None, None, None),
                        ):
                            output = await workflow_dbos.execute_registered_workflow(
                                host.host_id,
                                "wf",
                                f"mw:{host.host_id}:wf:test",
                                workflow_input={"target_agent_id": "primary"},
                            )

                        self.assertEqual(output["task_states"]["task"], {"ok": True})
                        worker = host.get_agent("worker")
                        sessions = await worker.list_sessions()
                        self.assertEqual(len(sessions), 1)
                        self.assertTrue(sessions[0]["session_id"].startswith("workflow:wf:task:task:run:"))
                    finally:
                        await host.close()

    async def test_provider_exception_is_surfaced_without_crossing_dbos_as_object(self) -> None:
        class FakeProviderError(RuntimeError):
            pass

        provider_message = (
            "Error code: 529 - {'type': 'error', 'error': "
            "{'type': 'overloaded_error', 'message': 'Overloaded'}}"
        )

        async def raise_provider_error(*_args, **_kwargs):
            raise FakeProviderError(provider_message)

        _FailureInspectingWorkflowDBOS.fail_payload = None
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch(
                    "mash.runtime.engine.workflow.DBOS",
                    _FailureInspectingWorkflowDBOS,
                ), patch(
                    "mash.runtime.engine.workflow.plan_request_step",
                    raise_provider_error,
                ):
                    host = HostBuilder().primary(
                        build_spec(agent_id="primary", response_text="unused")
                    ).build()
                    host.configure_runtime_database_url("postgresql://test/runtime")
                    await host.start()
                    try:
                        client = host.get_client("primary")
                        request_id = await client.post_request("hi", session_id="s-1")
                        events = await _collect_events(client, request_id, timeout=5)

                        self.assertEqual(events[-1]["event"], "request.error")
                        payload = events[-1]["data"]
                        self.assertEqual(payload["error"], provider_message)
                        self.assertEqual(payload["error_type"], "FakeProviderError")
                        self.assertEqual(
                            _FailureInspectingWorkflowDBOS.fail_payload,
                            {
                                "error": provider_message,
                                "error_type": "FakeProviderError",
                            },
                        )
                        self.assertNotIn("gASV", str(payload.get("error")))
                    finally:
                        await host.close()

    async def test_subagent_invocation_uses_real_runtime_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                host = (
                    HostBuilder()
                    .primary(
                        build_delegating_spec(
                            agent_id="primary-app",
                            final_text="delegated-ok",
                            subagent_id="research",
                            subagent_prompt="analyze",
                        )
                    )
                    .subagent(
                        build_spec(
                            agent_id="research",
                            response_text="research-ok",
                        ),
                        metadata=metadata(),
                    )
                    .build()
                )
                host.configure_runtime_database_url("postgresql://test/runtime")
                await host.start()
                try:
                    client = host.get_client("primary-app")
                    request_id = await client.post_request("delegate", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    research = host.get_agent("research")
                    expected_subagent_session = derive_subagent_session_id(
                        "primary-app",
                        "s-1",
                        "research",
                    )
                    turns = await research.store.get_turns(
                        session_id=expected_subagent_session,
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                    self.assertEqual(turns[-1]["metadata"]["primary_app_id"], "primary-app")
                    self.assertEqual(turns[-1]["metadata"]["primary_session_id"], "s-1")
                finally:
                    await host.close()

    async def test_subagent_invocation_starts_child_workflow_outside_step_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch("mash.runtime.engine.workflow.DBOS", _FakeWorkflowDBOS):
                    host = (
                        HostBuilder()
                        .primary(
                            build_delegating_spec(
                                agent_id="primary-app",
                                final_text="delegated-ok",
                                subagent_id="research",
                                subagent_prompt="analyze",
                            )
                        )
                        .subagent(
                            build_spec(
                                agent_id="research",
                                response_text="research-ok",
                            ),
                            metadata=metadata(),
                        )
                        .build()
                    )
                    host.configure_runtime_database_url("postgresql://test/runtime")
                    await host.start()
                    try:
                        client = host.get_client("primary-app")
                        request_id = await client.post_request("delegate", session_id="s-1")
                        self.assertTrue(request_id)

                        result = await _collect_terminal_payload(client, request_id, timeout=5)
                        self.assertEqual(result["response"]["text"], "delegated-ok")

                        primary = host.get_agent("primary-app")
                        request_events = await primary.runtime_store.list_request_events(request_id)
                        subagent_events = [
                            event
                            for event in request_events
                            if event.event_type == "runtime.subagent.call.completed"
                        ]
                        self.assertEqual(len(subagent_events), 1)
                        self.assertTrue(
                            subagent_events[0]
                            .payload["result"]["metadata"]
                            .get("request_id")
                        )

                        research = host.get_agent("research")
                        expected_subagent_session = derive_subagent_session_id(
                            "primary-app",
                            "s-1",
                            "research",
                        )
                        turns = await research.store.get_turns(
                            session_id=expected_subagent_session,
                            app_id=research.app_id,
                            limit=1,
                        )
                        self.assertEqual(turns[-1]["user_message"], "analyze")
                    finally:
                        await host.close()

    async def test_request_error_emits_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                host = HostBuilder().primary(
                    build_spec(
                        agent_id="primary",
                        response_text="ok",
                        fail_on_message="boom",
                    )
                ).build()
                host.configure_runtime_database_url("postgresql://test/runtime")
                await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("boom", session_id="s-1")
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"]["error"]))
                finally:
                    await host.close()

async def _collect_events(client, request_id: str, *, timeout: float) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async for event in client.stream_response(request_id, timeout=timeout):
        events.append(event)
        if event.get("event") in {"request.completed", "request.error"}:
            break
    return events


async def _collect_terminal_payload(
    client,
    request_id: str,
    *,
    timeout: float,
) -> dict[str, object]:
    events = await _collect_events(client, request_id, timeout=timeout)
    terminal = events[-1]
    if terminal.get("event") != "request.completed":
        raise AssertionError(f"expected request.completed, got {terminal.get('event')}")
    payload = terminal.get("data")
    if not isinstance(payload, dict):
        raise AssertionError("terminal payload must be a dict")
    return payload


if __name__ == "__main__":
    unittest.main()

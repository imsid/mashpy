"""Integration tests for pool-managed runtime server contracts."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from mash.runtime.client import AgentClientError
from mash.runtime.engine.dbos import register_runtime, require_runtime, unregister_runtime
from mash.runtime.engine.workflow import execute_request_workflow
from mash.runtime import Host, HostBuilder
from mash.skills import Skill
from mash.testing.runtime_fixtures import (
    build_delegating_spec,
    build_spec,
    metadata,
)
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec
from mash.workflows import dbos as workflow_dbos

_IN_FAKE_DBOS_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_fake_dbos_step",
    default=False,
)


class _CodeInput(BaseModel):
    value: str


def _code_passthrough(inp: _CodeInput, _ctx: StepContext) -> _CodeInput:
    return inp


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
    fail_payload: dict[str, Any] | None = None

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
        request_metadata: dict[str, Any],
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


class AgentPoolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._masher_llm_patch = patch(
            "mash.agents.masher.spec.EvalAgentSpec.build_llm",
            return_value=build_spec(
                agent_id="eval-agent", response_text="{}"
            ).build_llm(),
        )
        self._masher_llm_patch.start()

    def tearDown(self) -> None:
        self._masher_llm_patch.stop()

    def test_host_validation_rejects_unknown_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="primary", response_text="primary-ok"),
                        metadata=metadata(),
                    )
                    .build()
                )
                with self.assertRaises(ValueError):
                    pool.define_host(Host(host_id="h", primary="missing"))
                with self.assertRaises(ValueError):
                    pool.define_host(
                        Host(host_id="h", primary="primary", subagents=("missing",))
                    )
                with self.assertRaises(ValueError):
                    pool.define_host(
                        Host(host_id="h", primary="primary", workflows=("missing",))
                    )

    def test_host_type_rejects_invalid_composition(self) -> None:
        with self.assertRaises(ValueError):
            Host(host_id="", primary="primary")
        with self.assertRaises(ValueError):
            Host(host_id="h", primary="")
        with self.assertRaises(ValueError):
            Host(host_id="h", primary="primary", subagents=("primary",))
        with self.assertRaises(ValueError):
            Host(host_id="h", primary="primary", subagents=("a", "a"))

    def test_builder_composes_flat_pool_and_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="primary", response_text="primary-ok"),
                        metadata=metadata(),
                    )
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(
                            host_id="assistant",
                            primary="primary",
                            subagents=("research",),
                        )
                    )
                    .build()
                )
                described = {str(item["agent_id"]): item for item in pool.describe_agents()}
                self.assertEqual(
                    sorted(described.keys()),
                    ["eval-agent", "eval-judge-agent", "primary", "research"],
                )
                for item in described.values():
                    self.assertNotIn("role", item)
                    self.assertIsNotNone(item["metadata"])

                host = pool.get_host("assistant")
                self.assertEqual(host.primary, "primary")
                self.assertEqual(host.subagents, ("research",))
                self.assertEqual(
                    pool.snapshot_for(host),
                    {
                        "host_id": "assistant",
                        "primary": "primary",
                        "subagents": ["research"],
                    },
                )
                described_host = pool.describe_host("assistant")
                self.assertEqual(described_host["primary"]["agent_id"], "primary")
                self.assertEqual(
                    described_host["subagents"][0]["metadata"]["display_name"],
                    "Research",
                )

    def test_host_builder_registers_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_spec(agent_id="primary", response_text="primary-ok")
                pool = (
                    HostBuilder()
                    .agent(primary_spec, metadata=metadata())
                    .workflow(
                        WorkflowSpec(
                            workflow_id="changelog",
                            steps=[
                                AgentStep(
                                    step_id="scan",
                                    agent_spec=primary_spec,
                                    output={"type": "object"},
                                )
                            ],
                        )
                    )
                    .host(
                        Host(
                            host_id="changelog-host",
                            primary="primary",
                            workflows=("changelog",),
                        )
                    )
                    .build()
                )
                workflow_ids = {
                    item.workflow_id for item in pool.get_workflow_registry().list()
                }
                self.assertEqual(
                    workflow_ids,
                    {
                        "changelog",
                        "masher-trace-digest",
                        "masher-online-eval-curation",
                        "gen-synthetic-evals",
                        "run-experiment",
                    },
                )
                self.assertEqual(
                    pool.get_host("changelog-host").workflows,
                    (
                        "changelog",
                        "masher-trace-digest",
                        "masher-online-eval-curation",
                        "gen-synthetic-evals",
                        "run-experiment",
                    ),
                )

    def test_code_step_declared_agents_must_be_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                with self.assertRaisesRegex(
                    ValueError,
                    "workflow agent 'worker' declared by code step 'dispatch' is not registered",
                ):
                    (
                        HostBuilder()
                        .workflow(
                            WorkflowSpec(
                                workflow_id="dispatch-work",
                                input_model=_CodeInput,
                                steps=[
                                    CodeStep(
                                        step_id="dispatch",
                                        run=_code_passthrough,
                                        input=_CodeInput,
                                        output=_CodeInput,
                                        agent_ids=["worker"],
                                    )
                                ],
                            )
                        )
                        .build()
                    )

    def test_code_step_declared_agents_use_pool_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="worker", response_text="ok"),
                        metadata=metadata(),
                    )
                    .workflow(
                        WorkflowSpec(
                            workflow_id="dispatch-work",
                            input_model=_CodeInput,
                            steps=[
                                CodeStep(
                                    step_id="dispatch",
                                    run=_code_passthrough,
                                    input=_CodeInput,
                                    output=_CodeInput,
                                    agent_ids=["worker"],
                                )
                            ],
                        )
                    )
                    .build()
                )

                workflow = pool.get_workflow_registry().get("dispatch-work")
                self.assertEqual(workflow.steps[0].agent_ids, ("worker",))

    async def test_host_builder_registers_multiple_workflow_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                worker_a = build_spec(agent_id="worker-a", response_text="{}")
                worker_b = build_spec(agent_id="worker-b", response_text="{}")
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="primary", response_text="primary-ok"),
                        metadata=metadata(),
                    )
                    .workflow(
                        WorkflowSpec(
                            workflow_id="wf-a",
                            steps=[AgentStep(step_id="task-a", agent_spec=worker_a, output={"type": "object"})],
                        )
                    )
                    .workflow(
                        WorkflowSpec(
                            workflow_id="wf-b",
                            steps=[AgentStep(step_id="task-b", agent_spec=worker_b, output={"type": "object"})],
                        )
                    )
                    .build()
                )

                described = {str(item["agent_id"]): item for item in pool.describe_agents()}
                self.assertEqual(
                    sorted(described.keys()),
                    ["eval-agent", "eval-judge-agent", "primary"],
                )
                self.assertEqual(
                    pool.list_agents(), ["primary", "eval-agent", "eval-judge-agent"]
                )
                self.assertEqual(
                    sorted(item.workflow_id for item in pool.get_workflow_registry().list()),
                    [
                        "gen-synthetic-evals",
                        "masher-online-eval-curation",
                        "masher-trace-digest",
                        "run-experiment",
                        "wf-a",
                        "wf-b",
                    ],
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    self.assertIsNotNone(pool.get_client("worker-a"))
                    self.assertIsNotNone(pool.get_client("worker-b"))
                finally:
                    await pool.close()

    async def test_host_starts_runtime_servers_and_client_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = HostBuilder().agent(
                    build_spec(agent_id="primary", response_text="primary-ok"),
                    metadata=metadata(),
                ).build()
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    client = pool.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")

                    primary = pool.get_agent("primary")
                    sessions = await primary.list_sessions()
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["session_id"], "s-1")

                    definitions = primary.get_signal_definitions()
                    self.assertEqual(set(definitions.keys()), {"unused_tools", "unused_tool_tokens"})
                    self.assertEqual(definitions["unused_tools"]["value_type"], "string_list")
                    self.assertEqual(definitions["unused_tool_tokens"]["computed_at"], "turn_complete")

                    signal_rows = await primary.get_session_signals("s-1")
                    self.assertGreaterEqual(len(signal_rows), 1)
                    self.assertEqual(signal_rows[-1]["trace_id"], result["trace_id"])
                    self.assertIn("unused_tools", signal_rows[-1]["signals"])
                    self.assertIn("unused_tool_tokens", signal_rows[-1]["signals"])
                finally:
                    await pool.close()

    async def test_dynamic_skill_registered_before_start_is_available_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = HostBuilder().agent(
                    build_spec(agent_id="primary", response_text="primary-ok"),
                    metadata=metadata(),
                ).build()
                pool.register_agent_skill(
                    "primary",
                    Skill(
                        type="dynamic",
                        name="workflow:test:v1",
                        description="Test workflow skill.",
                        content="# Test workflow",
                    ),
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    runtime = pool.get_agent("primary")
                    self.assertIsNotNone(runtime.skills.get("workflow:test:v1"))
                    self.assertIn("Skill", runtime.agent.tools)
                finally:
                    await pool.close()

    async def test_dynamic_skill_registered_after_start_updates_live_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = HostBuilder().agent(
                    build_spec(agent_id="primary", response_text="primary-ok"),
                    metadata=metadata(),
                ).build()
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    runtime = pool.get_agent("primary")
                    self.assertIsNone(runtime.skills.get("workflow:test:v1"))
                    self.assertNotIn("Skill", runtime.agent.tools)

                    pool.register_agent_skill(
                        "primary",
                        Skill(
                            type="dynamic",
                            name="workflow:test:v1",
                            description="Test workflow skill.",
                            content="# Test workflow",
                        ),
                    )

                    self.assertIsNotNone(runtime.skills.get("workflow:test:v1"))
                    self.assertIn("Skill", runtime.agent.tools)
                    turn_agent = runtime.build_turn_agent(
                        session_id="s-1",
                        trace_id="trace-1",
                    )
                    try:
                        self.assertIsNotNone(turn_agent.skills.get("workflow:test:v1"))
                        self.assertIn("Skill", turn_agent.tools)
                    finally:
                        await turn_agent.tools.shutdown()

                    pool.unregister_agent_skill("primary", "workflow:test:v1")

                    self.assertIsNone(runtime.skills.get("workflow:test:v1"))
                    self.assertNotIn("Skill", runtime.agent.tools)
                finally:
                    await pool.close()

    async def test_host_start_does_not_self_probe_runtime_health_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = HostBuilder().agent(
                    build_spec(agent_id="primary", response_text="primary-ok"),
                    metadata=metadata(),
                ).build()
                with patch(
                    "mash.runtime.client.AgentClient.health",
                    side_effect=AgentClientError("unexpected health probe"),
                ):
                    pool.configure_runtime_database_url("postgresql://test/runtime")
                    await pool.start()
                try:
                    client = pool.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")
                finally:
                    await pool.close()

    async def test_host_exposes_workflow_service_for_registered_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_spec(
                    agent_id="primary",
                    response_text='{"last_run_ts":"2026-05-14T00:00:00Z"}',
                )
                pool = (
                    HostBuilder()
                    .agent(primary_spec, metadata=metadata())
                    .workflow(
                        WorkflowSpec(
                            workflow_id="changelog",
                            steps=[
                                AgentStep(
                                    step_id="scan-codebase-and-append-changelog",
                                    agent_spec=primary_spec,
                                    output={"type": "object"},
                                )
                            ],
                        )
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    workflow_service = pool.get_workflow_service()
                    self.assertIsNotNone(workflow_service)
                    listed = await workflow_service.list_workflows()
                    self.assertEqual(len(listed), 5)
                    self.assertIn("changelog", {item["workflow_id"] for item in listed})

                    async def start_workflow_run(**_kwargs):
                        return f"mw:{pool.runner_id}:changelog:abc"

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
                    await pool.close()

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
                    pool = (
                        HostBuilder()
                        .agent(
                            build_spec(agent_id="primary", response_text="primary-ok"),
                            metadata=metadata(),
                        )
                        .workflow(
                            WorkflowSpec(
                                workflow_id="wf",
                                steps=[
                                    AgentStep(
                                        step_id="task",
                                        agent_spec=worker_spec,
                                        output={"type": "object"},
                                    )
                                ],
                            )
                        )
                        .build()
                    )
                    pool.configure_runtime_database_url("postgresql://test/runtime")
                    await pool.start()
                    try:
                        with patch.object(
                            workflow_dbos,
                            "_load_dbos_api",
                            return_value=(_FakeWorkflowDBOS, None, None, None, None),
                        ), patch(
                            "mash.workflows.engine.load_dbos_api",
                            return_value=(_FakeWorkflowDBOS, None, None, None, None),
                        ):
                            output = await workflow_dbos.execute_registered_workflow(
                                pool.runner_id,
                                "wf",
                                f"mw:{pool.runner_id}:wf:test",
                                workflow_input={"target_agent_id": "primary"},
                                session_id="repl-session-1",
                            )

                        self.assertEqual(output["result"], {"ok": True})
                        run_id = f"mw:{pool.runner_id}:wf:test"
                        worker = pool.get_agent("worker")
                        sessions = await worker.list_sessions()
                        # The run executes under the threaded caller session (no
                        # synthetic workflow:...:run: scheme), tagged by run id.
                        self.assertEqual(len(sessions), 1)
                        self.assertEqual(sessions[0]["session_id"], "repl-session-1")

                        run_turns = await worker.memory_store.list_workflow_turns(
                            app_id="worker", workflow_id="wf"
                        )
                        self.assertEqual(len(run_turns), 1)
                        self.assertEqual(run_turns[0]["workflow_run_id"], run_id)
                        self.assertEqual(run_turns[0]["task_id"], "task")

                        # The run's trace events are queryable by workflow_run_id.
                        events = await worker.runtime_store.list_events(
                            app_id="worker", workflow_run_id=run_id
                        )
                        self.assertTrue(events)
                        self.assertTrue(
                            all(e.workflow_run_id == run_id for e in events)
                        )
                    finally:
                        await pool.close()

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
                async def _no_retry(fn, **_kw):
                    return await fn()

                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch(
                    "mash.runtime.engine.workflow.DBOS",
                    _FailureInspectingWorkflowDBOS,
                ), patch(
                    "mash.runtime.engine.workflow.plan_request_step",
                    raise_provider_error,
                ), patch(
                    "mash.runtime.engine.workflow.retry_transient",
                    _no_retry,
                ):
                    pool = HostBuilder().agent(
                        build_spec(agent_id="primary", response_text="unused"),
                        metadata=metadata(),
                    ).build()
                    pool.configure_runtime_database_url("postgresql://test/runtime")
                    await pool.start()
                    try:
                        client = pool.get_client("primary")
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
                                "error_code": "overloaded",
                                "retryable": True,
                            },
                        )
                        self.assertNotIn("gASV", str(payload.get("error")))
                    finally:
                        await pool.close()

    async def test_host_request_routes_subagent_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_delegating_spec(
                    agent_id="primary-app",
                    final_text="delegated-ok",
                    subagent_id="research",
                    subagent_prompt="analyze",
                )
                pool = (
                    HostBuilder()
                    .agent(primary_spec, metadata=metadata())
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(
                            host_id="assistant",
                            primary="primary-app",
                            subagents=("research",),
                        )
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    accepted = await pool.submit_host_request(
                        "assistant", message="delegate", session_id="s-1"
                    )
                    self.assertEqual(accepted["agent_id"], "primary-app")
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    # The subagent block must reach the LLM via the loaded
                    # context's system prompt.
                    self.assertIn("SUBAGENTS", str(primary_spec.provider.last_system))
                    self.assertIn("research", str(primary_spec.provider.last_system))

                    # Every event of a host-routed request is stamped with the
                    # composition's host_id, and the store can filter on it.
                    primary = pool.get_agent("primary-app")
                    request_events = await primary.runtime_store.list_request_events(
                        accepted["request_id"]
                    )
                    self.assertTrue(request_events)
                    for event in request_events:
                        self.assertEqual(event.host_id, "assistant")
                    filtered = await primary.runtime_store.list_events(
                        "primary-app", host_id="assistant"
                    )
                    self.assertTrue(filtered)
                    self.assertEqual(
                        await primary.runtime_store.list_events(
                            "primary-app", host_id="other-host"
                        ),
                        [],
                    )

                    research = pool.get_agent("research")
                    expected_subagent_session = "s-1"
                    turns = await research.store.get_turns(
                        session_id=expected_subagent_session,
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                    self.assertEqual(turns[-1]["metadata"]["primary_app_id"], "primary-app")
                    self.assertEqual(turns[-1]["metadata"]["primary_session_id"], "s-1")
                finally:
                    await pool.close()

    async def test_host_request_injects_caller_context_into_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = build_spec(
                    agent_id="primary-app", response_text="answered"
                )
                pool = (
                    HostBuilder()
                    .agent(primary_spec, metadata=metadata())
                    .host(Host(host_id="assistant", primary="primary-app"))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    accepted = await pool.submit_host_request(
                        "assistant",
                        message="hello",
                        session_id="s-ctx",
                        context="User timezone: PST. Workspace: mashpy.",
                    )
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "answered")

                    # The caller-supplied context must reach the LLM via the
                    # loaded context's system prompt.
                    self.assertIn(
                        "User timezone: PST. Workspace: mashpy.",
                        str(primary_spec.provider.last_system),
                    )
                finally:
                    await pool.close()

    async def test_dynamically_defined_host_routes_subagent_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                # No .host() at build time: the composition arrives after start().
                pool = (
                    HostBuilder()
                    .agent(
                        build_delegating_spec(
                            agent_id="primary-app",
                            final_text="delegated-ok",
                            subagent_id="research",
                            subagent_prompt="analyze",
                        ),
                        metadata=metadata(),
                    )
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    pool.define_host(
                        Host(
                            host_id="h-dyn",
                            primary="primary-app",
                            subagents=("research",),
                        )
                    )
                    accepted = await pool.submit_host_request(
                        "h-dyn", message="delegate", session_id="s-1"
                    )
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    research = pool.get_agent("research")
                    turns = await research.store.get_turns(
                        session_id="s-1",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                finally:
                    await pool.close()

    async def test_bare_agent_request_has_no_subagent_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_delegating_spec(
                            agent_id="primary-app",
                            final_text="delegated-ok",
                            subagent_id="research",
                            subagent_prompt="analyze",
                        ),
                        metadata=metadata(),
                    )
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(
                            host_id="assistant",
                            primary="primary-app",
                            subagents=("research",),
                        )
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    # Bare submit: same agent, no host snapshot, so the
                    # InvokeSubagent call must fail as an unknown tool.
                    client = pool.get_client("primary-app")
                    request_id = await client.post_request("delegate", session_id="s-2")
                    await _collect_events(client, request_id, timeout=5)

                    primary = pool.get_agent("primary-app")
                    request_events = await primary.runtime_store.list_request_events(
                        request_id
                    )
                    subagent_completions = [
                        event
                        for event in request_events
                        if event.event_type == "runtime.subagent.call.completed"
                    ]
                    for event in subagent_completions:
                        self.assertTrue(event.payload["result"]["is_error"])

                    # Bare requests carry no host_id on their events.
                    for event in request_events:
                        self.assertIsNone(event.host_id)

                    research = pool.get_agent("research")
                    turns = await research.store.get_turns(
                        session_id="s-2",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns, [])
                finally:
                    await pool.close()

    async def test_host_snapshot_gates_subagent_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                # The host's only subagent is "other"; the primary tries to
                # invoke "research", which is in the pool but not in the host.
                pool = (
                    HostBuilder()
                    .agent(
                        build_delegating_spec(
                            agent_id="primary-app",
                            final_text="delegated-ok",
                            subagent_id="research",
                            subagent_prompt="analyze",
                        ),
                        metadata=metadata(),
                    )
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .agent(
                        build_spec(agent_id="other", response_text="other-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(
                            host_id="narrow",
                            primary="primary-app",
                            subagents=("other",),
                        )
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    accepted = await pool.submit_host_request(
                        "narrow", message="delegate", session_id="s-1"
                    )
                    client = pool.get_client("primary-app")
                    await _collect_events(
                        client, accepted["request_id"], timeout=5
                    )
                    research = pool.get_agent("research")
                    turns = await research.store.get_turns(
                        session_id="s-1",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns, [])
                finally:
                    await pool.close()

    async def test_subagent_invocation_starts_child_workflow_outside_step_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch("mash.runtime.engine.workflow.DBOS", _FakeWorkflowDBOS):
                    pool = (
                        HostBuilder()
                        .agent(
                            build_delegating_spec(
                                agent_id="primary-app",
                                final_text="delegated-ok",
                                subagent_id="research",
                                subagent_prompt="analyze",
                            ),
                            metadata=metadata(),
                        )
                        .agent(
                            build_spec(
                                agent_id="research",
                                response_text="research-ok",
                            ),
                            metadata=metadata(),
                        )
                        .host(
                            Host(
                                host_id="assistant",
                                primary="primary-app",
                                subagents=("research",),
                            )
                        )
                        .build()
                    )
                    pool.configure_runtime_database_url("postgresql://test/runtime")
                    await pool.start()
                    try:
                        accepted = await pool.submit_host_request(
                            "assistant", message="delegate", session_id="s-1"
                        )
                        request_id = accepted["request_id"]
                        self.assertTrue(request_id)

                        client = pool.get_client("primary-app")
                        result = await _collect_terminal_payload(client, request_id, timeout=5)
                        self.assertEqual(result["response"]["text"], "delegated-ok")

                        primary = pool.get_agent("primary-app")
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

                        research = pool.get_agent("research")
                        expected_subagent_session = "s-1"
                        turns = await research.store.get_turns(
                            session_id=expected_subagent_session,
                            app_id=research.app_id,
                            limit=1,
                        )
                        self.assertEqual(turns[-1]["user_message"], "analyze")
                    finally:
                        await pool.close()

    async def test_request_error_emits_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = HostBuilder().agent(
                    build_spec(
                        agent_id="primary",
                        response_text="ok",
                        fail_on_message="boom",
                    ),
                    metadata=metadata(),
                ).build()
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    client = pool.get_client("primary")
                    request_id = await client.post_request("boom", session_id="s-1")
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"]["error"]))
                finally:
                    await pool.close()

async def _collect_events(client, request_id: str, *, timeout: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
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
) -> dict[str, Any]:
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

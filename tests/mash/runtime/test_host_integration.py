"""Integration tests for pool-managed runtime server contracts."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import (
    LLMContentBlock,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
)
from mash.logging import bound_request_id, bound_subagent_id_prefix, get_request_metadata
from mash.runtime.client import AgentClientError
from mash.runtime.engine.dbos import register_runtime, require_runtime, unregister_runtime
from mash.runtime.engine.workflow import execute_request_workflow
from mash.runtime import AgentSpec, Host, HostBuilder
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import (
    build_delegating_spec,
    build_spec,
    metadata,
)
from mash.testing.runtime_fixtures import DeterministicAgentSpec
from mash.tools.ask_user import AskUserTool
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry
from mash.tools.subagent import RunSubAgentsTool, child_request_id
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec
from mash.workflows import dbos as workflow_dbos

_IN_FAKE_DBOS_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_fake_dbos_step",
    default=False,
)


class _ForUser:
    """An agent client whose requests act for one user, as the relay's do."""

    def __init__(self, client: Any, user_id: str) -> None:
        self._client = client
        self._user_id = user_id

    async def post_request(self, *args: Any, **kwargs: Any) -> str:
        return await self._client.post_request(*args, user_id=self._user_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _AskingSpec(DeterministicAgentSpec):
    """A deterministic agent whose spec registers AskUser."""

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(AskUserTool())
        return tools


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


class _MetadataReadingTool:
    """Tool that records the caller metadata bound for the running request."""

    name = "read_metadata"
    description = "Read the bound request metadata."
    requires_approval = False
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        self.seen.append(get_request_metadata())
        return ToolResult.success("recorded")

    def to_llm_format(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class _ToolCallingLLMProvider(LLMProvider):
    """Calls read_metadata once, then finishes with a fixed answer."""

    def __init__(self, *, final_text: str) -> None:
        self._final_text = final_text
        self.last_session_id: str | None = None
        self.last_system: Any = None

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.last_system = request.system
        saw_tool_result = any(
            block.type == "tool_result"
            for message in request.messages
            if message.role == "tool"
            for block in message.content
        )
        if not saw_tool_result:
            return LLMResponse(
                thought_summary="",
                text="Reading metadata.",
                tool_calls=[
                    ToolCall(id="call-1", name="read_metadata", arguments={})
                ],
                content_blocks=[
                    LLMContentBlock.text("Reading metadata."),
                    LLMContentBlock.tool_call(
                        tool_call_id="call-1", name="read_metadata", arguments={}
                    ),
                ],
                stop_reason="tool_call",
                usage=LLMTokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
        return LLMResponse(
            thought_summary="",
            text=self._final_text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(self._final_text)],
            stop_reason="end_turn",
            usage=LLMTokenUsage(input_tokens=3, output_tokens=1, total_tokens=4),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, app_id
        self.last_session_id = session_id

    def set_trace_id(self, trace_id) -> None:
        del trace_id

    def get_event_logger_session_id(self) -> str | None:
        return self.last_session_id


class _MetadataReadingAgentSpec(AgentSpec):
    def __init__(self, *, agent_id: str, final_text: str = "meta-ok") -> None:
        self.agent_id = agent_id
        self.provider = _ToolCallingLLMProvider(final_text=final_text)
        self.tool = _MetadataReadingTool()

    def get_agent_id(self) -> str:
        return self.agent_id

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(self.tool)
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.provider

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.agent_id, system_prompt=f"You are {self.agent_id}."
        )


class PoolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._masher_llm_patch = patch(
            "mash.agents.masher.spec.EvalAgentSpec.build_llm",
            return_value=build_spec(
                agent_id="eval-agent", response_text="{}"
            ).build_llm(),
        )
        self._masher_judge_llm_patch = patch(
            "mash.agents.masher.spec.EvalJudgeAgentSpec.build_llm",
            return_value=build_spec(
                agent_id="eval-judge-agent", response_text="{}"
            ).build_llm(),
        )
        self._masher_llm_patch.start()
        self._masher_judge_llm_patch.start()

    def tearDown(self) -> None:
        self._masher_judge_llm_patch.stop()
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
                    ["primary", "research"],
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
                self.assertEqual(workflow_ids, {"changelog"})
                self.assertEqual(pool.get_host("changelog-host").workflows, ("changelog",))

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
                    ["primary"],
                )
                self.assertEqual(pool.list_agents(), ["primary"])
                self.assertEqual(
                    sorted(item.workflow_id for item in pool.get_workflow_registry().list()),
                    ["wf-a", "wf-b"],
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
                    request_id = await client.post_request("hello", session_id="s-1", user_id="u-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")

                    primary = _agent_runtime(pool, "primary")
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
                    request_id = await client.post_request("hello", session_id="s-1", user_id="u-1")
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
                    self.assertEqual(len(listed), 1)
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
                        run = await workflow_service.run_workflow("changelog", user_id="u-1")
                    self.assertEqual(run.status, "queued")
                finally:
                    await pool.close()

    async def test_workflow_agent_step_submits_through_the_client(self) -> None:
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
                                user_id="u-7",
                            )

                        self.assertEqual(output["result"], {"ok": True})
                        run_id = f"mw:{pool.runner_id}:wf:test"
                        worker = _agent_runtime(pool, "worker")
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
                        # The step submits under a request id fixed by run and step.
                        self.assertEqual(
                            {e.request_id for e in events}, {f"{run_id}-task"}
                        )
                        # ... and for the run's user.
                        identity = await worker.runtime_store.get_request_identity(
                            f"{run_id}-task"
                        )
                        self.assertEqual(identity.user_id, "u-7")
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
                        request_id = await client.post_request("hi", session_id="s-1", user_id="u-1")
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
                        "assistant", message="delegate", session_id="s-1",
                        user_id="u-1",
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
                    primary = _agent_runtime(pool, "primary-app")
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

                    research = _agent_runtime(pool, "research")
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
                        user_id="u-1",
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

    async def test_request_metadata_is_readable_by_tools_and_invisible_to_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = _MetadataReadingAgentSpec(agent_id="primary-app")
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
                        session_id="s-meta",
                        metadata={"tenant": "acme", "user_id": "u-1"},
                        user_id="u-1",
                    )
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "meta-ok")

                    # The tool observed the caller metadata during the run.
                    self.assertEqual(
                        primary_spec.tool.seen,
                        [{"tenant": "acme", "user_id": "u-1"}],
                    )
                    # Opaque to the model: metadata never enters the prompt.
                    self.assertNotIn("acme", str(primary_spec.provider.last_system))
                finally:
                    await pool.close()

    async def test_subagent_request_inherits_caller_metadata(self) -> None:
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
                        "assistant",
                        message="delegate",
                        session_id="s-1",
                        metadata={"tenant": "acme"},
                        user_id="u-1",
                    )
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    # The subagent request ran with the parent's caller metadata.
                    research = _agent_runtime(pool, "research")
                    turns = await research.store.get_turns(
                        session_id="s-1",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                    self.assertEqual(
                        turns[-1]["metadata"]["metadata"], {"tenant": "acme"}
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
                        "h-dyn", message="delegate", session_id="s-1",
                        user_id="u-1",
                    )
                    client = pool.get_client("primary-app")
                    result = await _collect_terminal_payload(
                        client, accepted["request_id"], timeout=5
                    )
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    research = _agent_runtime(pool, "research")
                    turns = await research.store.get_turns(
                        session_id="s-1",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                finally:
                    await pool.close()

    async def test_bare_agent_request_runs_children_only_on_itself(self) -> None:
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
                    # Bare submit: same agent, no host snapshot, so RunSubAgents
                    # may run children only on the calling agent.
                    client = pool.get_client("primary-app")
                    request_id = await client.post_request("delegate", session_id="s-2", user_id="u-1")
                    await _collect_events(client, request_id, timeout=5)

                    primary = _agent_runtime(pool, "primary-app")
                    request_events = await primary.runtime_store.list_request_events(
                        request_id
                    )
                    [completion] = [
                        event
                        for event in request_events
                        if event.event_type == "runtime.subagent.call.completed"
                    ]
                    [entry] = json.loads(completion.payload["result"]["content"])
                    self.assertEqual(entry["status"], "failed")
                    self.assertIsNone(entry["request_id"])
                    self.assertIn("RunSubAgents runs only on 'primary-app'", entry["error"])

                    # Bare requests carry no host_id on their events.
                    for event in request_events:
                        self.assertIsNone(event.host_id)

                    research = _agent_runtime(pool, "research")
                    turns = await research.store.get_turns(
                        session_id="s-2",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns, [])
                finally:
                    await pool.close()

    async def test_a_child_gets_neither_run_subagents_nor_ask_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        _AskingSpec(agent_id="primary-app", response_text="p"),
                        metadata=metadata(),
                    )
                    .agent(
                        _AskingSpec(agent_id="research", response_text="r"),
                        metadata=metadata(),
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    # A child carries the primary's host snapshot.
                    host = {"host_id": "assistant", "primary": "primary-app", "subagents": ["research"]}
                    primary = _agent_runtime(pool, "primary-app")
                    primary_agent = primary.build_turn_agent(
                        session_id="s-1", trace_id="t-1", host=host
                    )
                    research_agent = _agent_runtime(pool, "research").build_turn_agent(
                        session_id="s-1", trace_id="t-2", host=host, parent_kind="subagent"
                    )
                    own_child = primary.build_turn_agent(
                        session_id="s-1", trace_id="t-3", host=host, parent_kind="subagent"
                    )
                    try:
                        self.assertIn("RunSubAgents", primary_agent.tools)
                        self.assertIn("AskUser", primary_agent.tools)
                        for child in (research_agent, own_child):
                            self.assertNotIn("RunSubAgents", child.tools)
                            self.assertNotIn("AskUser", child.tools)
                            self.assertNotIn("SUBAGENTS", str(child.config.system_prompt))
                        self.assertIn("SUBAGENTS", str(primary_agent.config.system_prompt))
                        self.assertIn("- primary-app (you)", str(primary_agent.config.system_prompt))
                    finally:
                        await primary_agent.tools.shutdown()
                        await research_agent.tools.shutdown()
                        await own_child.tools.shutdown()
                finally:
                    await pool.close()

    async def _run_delegation(self, subagent_id: str) -> tuple[Any, str, dict[str, Any]]:
        pool = (
            HostBuilder()
            .agent(
                build_delegating_spec(
                    agent_id="primary-app",
                    final_text="delegated-ok",
                    subagent_id=subagent_id,
                    subagent_prompt="analyze",
                ),
                metadata=metadata(),
            )
            .agent(
                build_spec(agent_id="research", response_text="research-ok"),
                metadata=metadata(),
            )
            .host(Host(host_id="assistant", primary="primary-app", subagents=("research",)))
            .build()
        )
        pool.configure_runtime_database_url("postgresql://test/runtime")
        await pool.start()
        accepted = await pool.submit_host_request(
            "assistant", message="delegate", session_id="s-1", user_id="u-1"
        )
        request_id = accepted["request_id"]
        result = await _collect_terminal_payload(
            pool.get_client("primary-app"), request_id, timeout=10
        )
        return pool, request_id, result

    async def test_children_are_bg_requests_started_by_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool, request_id, result = await self._run_delegation("research")
                try:
                    self.assertEqual(result["response"]["text"], "delegated-ok")
                    store = pool.get_runtime_store()
                    [child] = await store.list_child_requests(request_id)
                    self.assertEqual(
                        {k: child[k] for k in ("agent_id", "mode", "started_by", "status", "user_id")},
                        {
                            "agent_id": "research",
                            "mode": "bg",
                            "started_by": "primary-app",
                            "status": "completed",
                            "user_id": "u-1",
                        },
                    )
                    self.assertEqual(child["parent_request_id"], request_id)
                    [parent] = [
                        r for r in await store.list_session_requests("s-1")
                        if r["request_id"] == request_id
                    ]
                    self.assertEqual((parent["mode"], parent["started_by"]), ("fg", "user"))
                finally:
                    await pool.close()

    async def test_the_caller_can_run_a_child_on_itself_through_the_relay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool, request_id, result = await self._run_delegation("primary-app")
                try:
                    self.assertEqual(result["response"]["text"], "delegated-ok")
                    store = pool.get_runtime_store()
                    [child] = await store.list_child_requests(request_id)
                    self.assertEqual(child["agent_id"], "primary-app")
                    self.assertEqual(child["started_by"], "primary-app")
                    self.assertEqual(child["status"], "completed")

                    # The child's own model asked for RunSubAgents too, but a
                    # child is built without it: splitting is one level deep.
                    child_events = await store.list_request_events(child["request_id"])
                    [call] = [
                        e for e in child_events
                        if e.event_type == "runtime.subagent.call.completed"
                    ]
                    self.assertTrue(call.payload["result"]["is_error"])
                    self.assertEqual(await store.list_child_requests(child["request_id"]), [])
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
                        "narrow", message="delegate", session_id="s-1",
                        user_id="u-1",
                    )
                    client = pool.get_client("primary-app")
                    await _collect_events(
                        client, accepted["request_id"], timeout=5
                    )
                    research = _agent_runtime(pool, "research")
                    turns = await research.store.get_turns(
                        session_id="s-1",
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns, [])
                finally:
                    await pool.close()

    async def test_subagent_call_inside_a_dbos_step_submits_over_http(self) -> None:
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
                            "assistant", message="delegate", session_id="s-1",
                            user_id="u-1",
                        )
                        request_id = accepted["request_id"]
                        self.assertTrue(request_id)

                        client = pool.get_client("primary-app")
                        result = await _collect_terminal_payload(client, request_id, timeout=5)
                        self.assertEqual(result["response"]["text"], "delegated-ok")

                        primary = _agent_runtime(pool, "primary-app")
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

                        research = _agent_runtime(pool, "research")
                        expected_subagent_session = "s-1"
                        turns = await research.store.get_turns(
                            session_id=expected_subagent_session,
                            app_id=research.app_id,
                            limit=1,
                        )
                        self.assertEqual(turns[-1]["user_message"], "analyze")
                    finally:
                        await pool.close()

    async def test_replayed_subagent_call_reattaches_to_the_same_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(build_spec(agent_id="primary-app", response_text="p"), metadata=metadata())
                    .agent(build_spec(agent_id="research", response_text="research-ok"), metadata=metadata())
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    # Called directly rather than through the host relay, which
                    # would set the parent request's user.
                    tool = RunSubAgentsTool(
                        client_resolver=lambda agent_id: _ForUser(
                            pool.get_client(agent_id), "u-1"
                        ),
                        caller_agent_id="primary-app",
                        session_id="s-1",
                    )

                    async def _call() -> dict[str, Any]:
                        # A replay re-runs the step under the same bound prefix.
                        with bound_request_id("req-1"), bound_subagent_id_prefix(
                            "req-1-sub-0-0"
                        ):
                            result = await tool.execute(
                                {"requests": [{"agent_id": "research", "prompt": "analyze"}]}
                            )
                        self.assertFalse(result.is_error, result.content)
                        [entry] = json.loads(result.content)
                        return entry

                    first = await _call()
                    second = await _call()

                    child_id = child_request_id("req-1-sub-0-0", 0)
                    self.assertEqual(first["request_id"], child_id)
                    self.assertEqual(second["request_id"], child_id)
                    self.assertEqual(second["response"], "research-ok")
                    research = _agent_runtime(pool, "research")
                    accepted = [
                        e
                        for e in await research.runtime_store.list_request_events(child_id)
                        if e.event_type == "runtime.request.accepted"
                    ]
                    self.assertEqual(len(accepted), 1)
                    metadata_ = accepted[0].payload["request_metadata"]
                    self.assertEqual(metadata_["subagent_id"], "research")
                    self.assertEqual(metadata_["parent"]["request_id"], "req-1")
                    turns = await research.store.get_turns(
                        session_id="s-1", app_id="research"
                    )
                    self.assertEqual(len(turns), 1)
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
                    request_id = await client.post_request("boom", session_id="s-1", user_id="u-1")
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"]["error"]))
                finally:
                    await pool.close()

def _agent_runtime(pool, agent_id: str):
    """The runtime serving ``agent_id`` in-process."""
    return pool._agent_provider.agent_servers[agent_id].runtime


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

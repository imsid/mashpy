"""Integration tests for pool-managed runtime server contracts."""

from __future__ import annotations

import asyncio
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
from mash.logging import (
    bound_request_id,
    bound_subagent_id_prefix,
    get_event_session_id,
    get_request_metadata,
    get_session_id,
)
from mash.runtime.client import AgentClientError
from mash.runtime import AgentSpec, Host, HostBuilder
from mash.skills.registry import SkillRegistry
from mash.testing.runtime_fixtures import (
    DEFAULT_POLICY,
    TEST_IDENTITY,
    RoutingLLMProvider,
    build_spec,
    metadata,
    patch_companion_llm,
)
from mash.testing.runtime_fixtures import (
    DeterministicAgentSpec,
    DeterministicLLMProvider,
    ToolCallingAgentSpec,
)
from mash.tools.ask_user import AskUserTool
from mash.tools.base import ToolResult
from mash.tools.registry import ToolRegistry
from mash.tools.subagent import RunSubAgentsTool, child_request_id
from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec

class _ForApp:
    """An agent client whose requests run for one app and policy, as the relay's do."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def post_request(self, *args: Any, **kwargs: Any) -> str:
        return await self._client.post_request(*args, **TEST_IDENTITY, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _RecordingProvider(DeterministicLLMProvider):
    """Records the text of every message each request sends the model."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: list[str] = []

    async def send(self, request):
        self.seen.append(
            "\n".join(
                str(block.data.get("text", ""))
                for message in request.messages
                for block in message.content
                if block.type == "text"
            )
        )
        return await super().send(request)


class _RecordingSpec(DeterministicAgentSpec):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.provider = _RecordingProvider(response_text=self.response_text)


class _AskingSpec(DeterministicAgentSpec):
    """A deterministic agent whose spec registers AskUser."""

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(AskUserTool())
        return tools


class _AskingChildSpec(ToolCallingAgentSpec):
    """Asks the user one question with AskUser, then answers with the reply."""

    def __init__(self, *, agent_id: str) -> None:
        super().__init__(agent_id=agent_id, tool_name="AskUser", arguments={"question": "Which one?"})

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(AskUserTool())
        return tools


class _DeployTool:
    """A tool that needs the user's approval."""

    name = "deploy"
    description = "Deploy."
    requires_approval = True
    parallel_safe = False
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        return ToolResult.success("deployed")

    def to_llm_format(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


class _DeployingChildSpec(ToolCallingAgentSpec):
    """Calls the approval-gated deploy tool, then answers with its result."""

    def __init__(self, *, agent_id: str) -> None:
        super().__init__(agent_id=agent_id, tool_name="deploy", arguments={})

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(_DeployTool())
        return tools


class _CodeInput(BaseModel):
    value: str


def _code_passthrough(inp: _CodeInput, _ctx: StepContext) -> _CodeInput:
    return inp


class _MetadataReadingTool:
    """Tool that records the caller metadata bound for the running request."""

    name = "read_metadata"
    description = "Read the bound request metadata."
    requires_approval = False
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.sessions: list[tuple[str | None, str | None]] = []

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        del args
        self.seen.append(get_request_metadata())
        self.sessions.append((get_session_id(), get_event_session_id()))
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

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


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
            agent_id=self.agent_id, system_prompt=f"You are {self.agent_id}."
        )


class _CompanionLLM(LLMProvider):
    """The companion's model in these tests; ``inner`` decides what it does."""

    def __init__(self) -> None:
        self.inner: LLMProvider = RoutingLLMProvider("research")

    @property
    def model(self) -> str:
        return "test-model"

    @property
    def last_system(self) -> Any:
        return getattr(self.inner, "last_system", None)

    async def send(self, request):
        return await self.inner.send(request)

    def set_event_logger(self, logger, agent_id: str) -> None:
        del logger, agent_id


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
        # The companion routes every host request; by default its classifier
        # picks "research".
        self.companion = _CompanionLLM()
        self._companion_patch = patch_companion_llm(self.companion)
        self._companion_patch.start()

    def tearDown(self) -> None:
        self._companion_patch.stop()
        self._masher_judge_llm_patch.stop()
        self._masher_llm_patch.stop()

    def _route(self, agent_id: str) -> RoutingLLMProvider:
        self.companion.inner = RoutingLLMProvider(agent_id)
        return self.companion.inner

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
                    pool.define_host(Host(host_id="h", agents=("missing",)))
                with self.assertRaises(ValueError):
                    pool.define_host(Host(host_id="h", agents=("primary", "missing")))
                with self.assertRaises(ValueError):
                    pool.define_host(Host(host_id="h", agents=("primary",), workflows=("missing",)))
                # The companion routes every host; listing it is refused.
                with self.assertRaises(ValueError):
                    pool.define_host(Host(host_id="h", agents=("companion",)))

    def test_host_type_rejects_invalid_composition(self) -> None:
        with self.assertRaises(ValueError):
            Host(host_id="", agents=("a",))
        with self.assertRaises(ValueError):
            Host(host_id="h", agents=("",))
        with self.assertRaises(ValueError):
            Host(host_id="h", agents=("a", "a"))

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
                    .host(Host(host_id="assistant", agents=("primary", "research")))
                    .build()
                )
                described = {str(item["agent_id"]): item for item in pool.describe_agents()}
                self.assertEqual(
                    sorted(described.keys()),
                    ["companion", "primary", "research"],
                )
                for item in described.values():
                    self.assertNotIn("role", item)
                    self.assertIsNotNone(item["metadata"])

                host = pool.get_host("assistant")
                self.assertEqual(host.agents, ("primary", "research"))
                self.assertEqual(
                    pool.snapshot_for(host),
                    {
                        "host_id": "assistant",
                        "agents": ["primary", "research"],
                        "workflows": [],
                    },
                )
                described_host = pool.describe_host("assistant")
                self.assertEqual(
                    [member["agent_id"] for member in described_host["agents"]],
                    ["primary", "research"],
                )
                self.assertEqual(
                    described_host["agents"][1]["metadata"]["display_name"],
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
                            agents=("primary",),
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
                    ["companion", "primary"],
                )
                self.assertEqual(pool.list_agents(), ["primary", "companion"])
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
                    request_id = await client.post_request("hello", session_id="s-1", **TEST_IDENTITY)
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
                    request_id = await client.post_request("hello", session_id="s-1", **TEST_IDENTITY)
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

                    run = await workflow_service.run_workflow("changelog", app_id="app-1", policy_id="p-1")
                    self.assertEqual(run.status, "queued")
                    self.assertTrue(run.run_id.startswith(f"mw:{pool.runner_id}:changelog:"))
                finally:
                    await pool.close()

    async def test_workflow_agent_step_submits_through_the_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
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
                    service = pool.get_workflow_service()
                    run = await service.run_workflow(
                        "wf",
                        workflow_input={"target_agent_id": "primary"},
                        session_id="repl-session-1",
                        app_id="app-7",
                        policy_id="p-1",
                    )
                    run_id = run.run_id
                    for _ in range(250):
                        finished = await service.get_run("wf", run_id)
                        if finished.status in ("completed", "failed"):
                            break
                        await asyncio.sleep(0.02)

                    self.assertEqual(finished.status, "completed")
                    self.assertEqual(finished.result, {"ok": True})
                    worker = _agent_runtime(pool, "worker")
                    sessions = await worker.list_sessions()
                    # The run executes under the threaded caller session (no
                    # synthetic workflow:...:run: scheme), tagged by run id.
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["session_id"], "repl-session-1")

                    run_turns = await worker.memory_store.list_workflow_turns(
                        agent_id="worker", workflow_id="wf"
                    )
                    self.assertEqual(len(run_turns), 1)
                    self.assertEqual(run_turns[0]["workflow_run_id"], run_id)
                    self.assertEqual(run_turns[0]["task_id"], "task")

                    # The run's trace events are queryable by workflow_run_id.
                    events = await worker.runtime_store.list_events(
                        agent_id="worker", workflow_run_id=run_id
                    )
                    self.assertTrue(events)
                    self.assertTrue(
                        all(e.workflow_run_id == run_id for e in events)
                    )
                    # The step submits under a request id fixed by run and step.
                    self.assertEqual(
                        {e.request_id for e in events}, {f"{run_id}-task"}
                    )
                    # ... for the run's app, under its policy.
                    identity = await worker.runtime_store.get_request_identity(
                        f"{run_id}-task"
                    )
                    self.assertEqual(
                        (identity.app_id, identity.policy_id, identity.policy),
                        ("app-7", "p-1", DEFAULT_POLICY),
                    )
                finally:
                    await pool.close()

    async def test_provider_exception_is_surfaced_on_the_terminal_event(self) -> None:
        class FakeProviderError(RuntimeError):
            pass

        provider_message = (
            "Error code: 529 - {'type': 'error', 'error': "
            "{'type': 'overloaded_error', 'message': 'Overloaded'}}"
        )

        async def raise_provider_error(*_args, **_kwargs):
            raise FakeProviderError(provider_message)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                async def _no_retry(fn, **_kw):
                    return await fn()

                with patch(
                    "mash.runtime.engine.loop.plan_request_step",
                    raise_provider_error,
                ), patch(
                    "mash.runtime.engine.loop.retry_transient",
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
                        request_id = await client.post_request("hi", session_id="s-1", **TEST_IDENTITY)
                        events = await _collect_events(client, request_id, timeout=5)

                        self.assertEqual(events[-1]["event"], "request.error")
                        payload = events[-1]["data"]
                        self.assertEqual(payload["error"], provider_message)
                        self.assertEqual(payload["error_type"], "FakeProviderError")
                        self.assertEqual(payload["error_code"], "overloaded")
                        self.assertIs(payload["retryable"], True)
                    finally:
                        await pool.close()

    async def _routed(self, pool: Any, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The companion's route for ``request_id`` and the routed request's terminal payload."""
        companion = await _collect_terminal_payload(
            pool.get_client("companion"), request_id, timeout=5
        )
        route = companion["response"]["metadata"]["routed"]
        result = await _collect_terminal_payload(
            pool.get_client(route["agent_id"]), route["request_id"], timeout=5
        )
        return route, result

    async def test_host_request_routes_to_an_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                router = self._route("research")
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(host_id="assistant", agents=("research",))
                    )
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    accepted = await pool.submit_host_request(
                        "assistant", message="delegate", session_id="s-1",
                        app_id="app-1", policy_id="p-1",
                    )
                    self.assertEqual(accepted["agent_id"], "companion")
                    route, result = await self._routed(pool, accepted["request_id"])
                    self.assertEqual(route["agent_id"], "research")
                    self.assertEqual(result["response"]["text"], "research-ok")

                    # The classifier reads the host's agents.
                    text = router.requests[0].messages[0].content[0].data["text"]
                    self.assertIn("AGENTS\n- research", text)

                    # Every event of a host-routed request is stamped with the
                    # composition's host_id, and the store can filter on it.
                    store = pool.get_runtime_store()
                    for request_id in (accepted["request_id"], route["request_id"]):
                        request_events = await store.list_request_events(request_id)
                        self.assertTrue(request_events)
                        for event in request_events:
                            self.assertEqual(event.host_id, "assistant")
                    self.assertTrue(await store.list_events("companion", host_id="assistant"))
                    self.assertEqual(
                        await store.list_events("companion", host_id="other-host"), []
                    )

                    # The routed request answered in the conversation's session.
                    research = _agent_runtime(pool, "research")
                    [entry] = await research.store.read_entries("s-1")
                    self.assertEqual(
                        (entry["agent_id"], entry["user_message"]), ("research", "delegate")
                    )
                finally:
                    await pool.close()

    async def test_host_request_passes_caller_context_to_the_routed_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                self._route("primary-app")
                spec = build_spec(agent_id="primary-app", response_text="answered")
                pool = (
                    HostBuilder()
                    .agent(spec, metadata=metadata())
                    .host(Host(host_id="assistant", agents=("primary-app",)))
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
                        app_id="app-1", policy_id="p-1",
                    )
                    _, result = await self._routed(pool, accepted["request_id"])
                    self.assertEqual(result["response"]["text"], "answered")

                    # The caller's context reaches the routed agent's model via
                    # its loaded system prompt.
                    self.assertIn(
                        "User timezone: PST. Workspace: mashpy.",
                        str(spec.provider.last_system),
                    )
                finally:
                    await pool.close()

    async def test_request_metadata_is_readable_by_tools_and_invisible_to_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                primary_spec = _MetadataReadingAgentSpec(agent_id="primary-app")
                pool = HostBuilder().agent(primary_spec, metadata=metadata()).build()
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    client = pool.get_client("primary-app")
                    request_id = await client.post_request(
                        "hello",
                        session_id="s-meta",
                        metadata={"tenant": "acme", "app_id": "app-1", "policy_id": "p-1"},
                        **TEST_IDENTITY,
                    )
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "meta-ok")

                    # The tool observed the caller metadata during the run.
                    self.assertEqual(
                        primary_spec.tool.seen,
                        [{"tenant": "acme", "app_id": "app-1", "policy_id": "p-1"}],
                    )
                    # Opaque to the model: metadata never enters the prompt.
                    self.assertNotIn("acme", str(primary_spec.provider.last_system))
                finally:
                    await pool.close()

    async def test_a_routed_request_inherits_caller_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(
                        Host(host_id="assistant", agents=("research",))
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
                        app_id="app-1", policy_id="p-1",
                    )
                    await self._routed(pool, accepted["request_id"])

                    research = _agent_runtime(pool, "research")
                    [entry] = await research.store.read_entries("s-1")
                    self.assertEqual(entry["metadata"]["metadata"], {"tenant": "acme"})
                finally:
                    await pool.close()

    async def test_a_routed_tool_reads_the_event_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                research = _MetadataReadingAgentSpec(agent_id="research")
                pool = (
                    HostBuilder()
                    .agent(research, metadata=metadata())
                    .host(Host(host_id="assistant", agents=("research",)))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    accepted = await pool.submit_host_request(
                        "assistant",
                        message="delegate",
                        session_id="s-1",
                        app_id="app-1", policy_id="p-1",
                    )
                    await self._routed(pool, accepted["request_id"])

                    # A routed request runs in the conversation's own session.
                    self.assertEqual(research.tool.sessions, [("s-1", "s-1")])
                finally:
                    await pool.close()

    async def test_dynamically_defined_host_routes_to_an_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                # No .host() at build time: the composition arrives after start().
                pool = (
                    HostBuilder()
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
                        Host(host_id="h-dyn", agents=("research",))
                    )
                    accepted = await pool.submit_host_request(
                        "h-dyn", message="delegate", session_id="s-1",
                        app_id="app-1", policy_id="p-1",
                    )
                    _, result = await self._routed(pool, accepted["request_id"])
                    self.assertEqual(result["response"]["text"], "research-ok")
                finally:
                    await pool.close()

    async def test_a_bare_companion_request_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        build_spec(agent_id="research", response_text="research-ok"),
                        metadata=metadata(),
                    )
                    .host(Host(host_id="assistant", agents=("research",)))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    # Bare submit: no host snapshot, so there is nothing to route to.
                    client = pool.get_client("companion")
                    request_id = await client.post_request("delegate", session_id="s-2", **TEST_IDENTITY)
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("routes only host requests", str(events[-1]["data"]["error"]))
                    store = pool.get_runtime_store()
                    self.assertEqual(await store.list_child_requests(request_id), [])
                    # Bare requests carry no host_id on their events.
                    for event in await store.list_request_events(request_id):
                        self.assertIsNone(event.host_id)
                finally:
                    await pool.close()

    async def test_no_agent_routes_and_a_routed_request_keeps_ask_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool = (
                    HostBuilder()
                    .agent(
                        _AskingSpec(agent_id="research", response_text="r"),
                        metadata=metadata(),
                    )
                    .host(Host(host_id="assistant", agents=("research",)))
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    host = pool.snapshot_for(pool.get_host("assistant"))
                    research = _agent_runtime(pool, "research")
                    direct = research.build_turn_agent(session_id="s-1", trace_id="t-2")
                    routed = research.build_turn_agent(
                        session_id="s-1", trace_id="t-3", host=host, parent_kind="conversation"
                    )
                    for agent in (direct, routed):
                        self.assertNotIn("RunSubAgents", agent.tools)
                        self.assertNotIn("AGENTS\n", str(agent.config.system_prompt))
                        # The user answers a routed agent's question directly.
                        self.assertIn("AskUser", agent.tools)
                    self.assertIn("read_board", routed.tools)
                    self.assertNotIn("read_board", direct.tools)
                finally:
                    await pool.close()

    async def _ask_on_routed(
        self, spec: AgentSpec, answer: Any
    ) -> tuple[Any, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Run a host request routed to an agent that opens one interaction; answer it there.

        Returns the pool, the route, the routed request's
        ``request.interaction.create`` and its stream.
        """
        self._route("research")
        pool = (
            HostBuilder()
            .agent(spec, metadata=metadata())
            .host(Host(host_id="assistant", agents=("research",)))
            .build()
        )
        pool.configure_runtime_database_url("postgresql://test/runtime")
        await pool.start()
        accepted = await pool.submit_host_request(
            "assistant", message="delegate", session_id="s-1", app_id="app-1", policy_id="p-1"
        )
        companion = await _collect_terminal_payload(
            pool.get_client("companion"), accepted["request_id"], timeout=5
        )
        route = companion["response"]["metadata"]["routed"]
        client = pool.get_client("research")
        opened: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        async for event in client.stream_response(route["request_id"], timeout=10):
            events.append(event)
            if event.get("event") == "request.interaction.create":
                opened = event["data"]
                await client.post_interaction(
                    route["request_id"], interaction_id=opened["interaction_id"], response=answer
                )
            if event.get("event") in {"request.completed", "request.error"}:
                break
        return pool, route, opened, events

    async def test_a_routed_agents_question_is_its_own_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool, route, opened, events = await self._ask_on_routed(
                    _AskingChildSpec(agent_id="research"), "the blue one"
                )
                try:
                    self.assertEqual(events[-1]["event"], "request.completed")
                    self.assertEqual(events[-1]["data"]["response"]["text"], "the blue one")
                    self.assertEqual((opened["type"], opened["prompt"]), ("info", "Which one?"))
                    # Nothing is relayed: the companion's request holds no interaction.
                    store = pool.get_runtime_store()
                    identity = await store.get_request_identity(route["request_id"])
                    companion_events = await store.list_request_events(identity.parent_request_id)
                    self.assertTrue(companion_events)
                    self.assertFalse(
                        [e for e in companion_events if e.event_type == "mash.interaction.start"]
                    )
                finally:
                    await pool.close()

    async def test_a_routed_agents_approval_is_its_own_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool, _, opened, events = await self._ask_on_routed(
                    _DeployingChildSpec(agent_id="research"), "approve"
                )
                try:
                    self.assertEqual(events[-1]["event"], "request.completed")
                    self.assertEqual(opened["type"], "approval")
                    self.assertEqual(opened["prompt"], "Approve execution of: deploy?")
                    self.assertEqual(opened["schema"]["options"], ["approve", "deny", "skip"])
                    # The tool ran only because the user's approve reached the agent.
                    self.assertEqual(events[-1]["data"]["response"]["text"], "deployed")
                finally:
                    await pool.close()

    async def _run_route(self, agent_id: str) -> tuple[Any, str, list[dict[str, Any]]]:
        self._route(agent_id)
        pool = (
            HostBuilder()
            .agent(
                build_spec(agent_id="research", response_text="research-ok"),
                metadata=metadata(),
            )
            .agent(
                build_spec(agent_id="other", response_text="other-ok"),
                metadata=metadata(),
            )
            .host(Host(host_id="assistant", agents=("research",)))
            .build()
        )
        pool.configure_runtime_database_url("postgresql://test/runtime")
        await pool.start()
        accepted = await pool.submit_host_request(
            "assistant", message="delegate", session_id="s-1", app_id="app-1", policy_id="p-1"
        )
        request_id = accepted["request_id"]
        events = await _collect_events(pool.get_client("companion"), request_id, timeout=10)
        return pool, request_id, events

    async def test_the_routed_request_is_started_by_the_companion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                pool, request_id, events = await self._run_route("research")
                try:
                    route = events[-1]["data"]["response"]["metadata"]["routed"]
                    await _collect_terminal_payload(
                        pool.get_client("research"), route["request_id"], timeout=5
                    )
                    store = pool.get_runtime_store()
                    [child] = await store.list_child_requests(request_id)
                    self.assertEqual(
                        {
                            k: child[k]
                            for k in (
                                "agent_id", "started_by", "status", "app_id", "policy_id",
                                "session_id",
                            )
                        },
                        {
                            "agent_id": "research",
                            "started_by": "companion",
                            "status": "completed",
                            "app_id": "app-1",
                            "policy_id": "p-1",
                            "session_id": "s-1",
                        },
                    )
                    self.assertEqual(child["parent_request_id"], request_id)
                    [parent] = [
                        r for r in await store.list_session_requests("s-1")
                        if r["request_id"] == request_id
                    ]
                    self.assertEqual(parent["started_by"], "user")
                finally:
                    await pool.close()

    async def test_a_route_outside_the_host_fails(self) -> None:
        # The companion itself, and an agent of the pool the host does not
        # list, are not routes.
        for agent_id in ("companion", "other"):
            with self.subTest(agent_id=agent_id), tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                    pool, request_id, events = await self._run_route(agent_id)
                    try:
                        self.assertEqual(events[-1]["event"], "request.error")
                        self.assertIn("not a route", str(events[-1]["data"]["error"]))
                        self.assertEqual(
                            await pool.get_runtime_store().list_child_requests(request_id), []
                        )
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
                    # would set the parent request's app and policy.
                    tool = RunSubAgentsTool(
                        client_resolver=lambda agent_id: _ForApp(pool.get_client(agent_id)),
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
                        if e.event_type == "invoke_agent.start"
                    ]
                    self.assertEqual(len(accepted), 1)
                    metadata_ = accepted[0].payload["request_metadata"]
                    self.assertEqual(metadata_["subagent_id"], "research")
                    self.assertEqual(metadata_["parent"]["request_id"], "req-1")
                    turns = await research.store.get_turns(
                        session_id=child_id, agent_id="research"
                    )
                    self.assertEqual(len(turns), 1)
                finally:
                    await pool.close()

    async def test_a_child_continues_its_session_and_never_sees_another(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}):
                research_spec = _RecordingSpec(agent_id="research", response_text="research-ok")
                pool = (
                    HostBuilder()
                    .agent(build_spec(agent_id="primary-app", response_text="p"), metadata=metadata())
                    .agent(research_spec, metadata=metadata())
                    .build()
                )
                pool.configure_runtime_database_url("postgresql://test/runtime")
                await pool.start()
                try:
                    tool = RunSubAgentsTool(
                        client_resolver=lambda agent_id: _ForApp(pool.get_client(agent_id)),
                        caller_agent_id="primary-app",
                        session_id="s-1",
                    )

                    async def _call(prefix: str, item: dict[str, Any]) -> dict[str, Any]:
                        with bound_request_id("req-1"), bound_subagent_id_prefix(prefix):
                            result = await tool.execute({"requests": [item]})
                        [entry] = json.loads(result.content)
                        self.assertEqual(entry["status"], "completed", entry)
                        return entry

                    rss = await _call("p1", {"agent_id": "research", "prompt": "build rss"})
                    csv = await _call("p2", {"agent_id": "research", "prompt": "build csv"})
                    self.assertNotEqual(rss["session_id"], csv["session_id"])
                    # Each new session starts empty: the csv child saw no rss work.
                    self.assertNotIn("build rss", research_spec.provider.seen[-1])

                    follow = await _call(
                        "p3",
                        {"agent_id": "research", "prompt": "add a test", "session_id": rss["session_id"]},
                    )

                    self.assertEqual(follow["session_id"], rss["session_id"])
                    self.assertTrue(follow["trace_id"])
                    seen = research_spec.provider.seen[-1]
                    self.assertIn("build rss", seen)
                    self.assertIn("add a test", seen)
                    self.assertNotIn("build csv", seen)
                    research = _agent_runtime(pool, "research")
                    turns = await research.store.get_turns(
                        session_id=rss["session_id"], agent_id="research"
                    )
                    self.assertEqual(len(turns), 2)
                    self.assertTrue(all(turn["replayable"] for turn in turns))
                    # Child turns are not the app's own conversation.
                    self.assertEqual(await research.store.list_app_turns("app-1"), [])
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
                    request_id = await client.post_request("boom", session_id="s-1", **TEST_IDENTITY)
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"]["error"]))
                finally:
                    await pool.close()

async def _child_session(runtime, parent_request_id: str) -> str:
    """The session the latest child of ``parent_request_id`` ran in."""
    [*_, child] = await runtime.runtime_store.list_child_requests(parent_request_id)
    # A child without a session_id starts one named after its request id.
    assert child["session_id"] == child["request_id"]
    return child["session_id"]


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

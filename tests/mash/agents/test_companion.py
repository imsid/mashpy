"""The companion: the built-in router of every host request."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from mash.agents.companion import COMPANION_AGENT_ID, CompanionSpec, build_companion_metadata
from mash.core.llm import AnthropicProvider
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import DelegatingLLMProvider, build_spec, metadata


def _builder() -> HostBuilder:
    return (
        HostBuilder()
        .agent(build_spec(agent_id="pilot"), metadata=metadata())
        .agent(build_spec(agent_id="builder"), metadata=metadata())
    )


class HostAgentsTests(unittest.TestCase):
    def test_every_pool_has_the_companion_and_hosts_list_only_their_agents(self) -> None:
        pool = _builder().host(Host(host_id="guide", agents=("pilot", "builder"))).build()

        host = pool.get_host("guide")
        self.assertEqual(host.agents, ("pilot", "builder"))
        self.assertEqual(
            pool.snapshot_for(host), {"host_id": "guide", "agents": ["pilot", "builder"], "workflows": []}
        )
        self.assertIn(COMPANION_AGENT_ID, pool.list_registered_agents())
        self.assertIn(COMPANION_AGENT_ID, HostBuilder().build().list_registered_agents())

    def test_define_host_on_a_running_pool(self) -> None:
        pool = _builder().build()
        defined = pool.define_host(Host(host_id="later", agents=("builder",)))
        self.assertEqual(defined.agents, ("builder",))

    def test_a_host_cannot_list_the_companion(self) -> None:
        with self.assertRaisesRegex(ValueError, "routes every host request"):
            _builder().host(Host(host_id="g", agents=(COMPANION_AGENT_ID,))).build()

    def test_disabling_the_companion_is_refused_while_a_host_exists(self) -> None:
        pool = _builder().host(Host(host_id="guide", agents=("pilot",))).build()
        self.assertEqual(pool.agent_references(COMPANION_AGENT_ID), ["host 'guide'"])
        self.assertEqual(pool.agent_references("builder"), [])


class CompanionSpecTests(unittest.TestCase):
    def test_spec_shape(self) -> None:
        spec = CompanionSpec()
        config = spec.build_agent_config()

        self.assertEqual(spec.get_agent_id(), "companion")
        self.assertEqual(config.app_id, "companion")
        self.assertEqual(config.conversation_history_turns, 10)
        self.assertFalse(spec.enable_sandbox())
        self.assertEqual(spec.browser_sites(), [])
        self.assertEqual(spec.build_skills().list_skills(), [])
        self.assertIn("in the AGENTS list", config.system_prompt)
        self.assertIn("session_id", config.system_prompt)
        self.assertTrue(build_companion_metadata().description)

    def test_low_thinking_on_gemini_only(self) -> None:
        base = {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "OSS_BASE_URL": ""}
        cases = [
            ({"GEMINI_API_KEY": "k"}, {"thinking_level": "low"}),
            ({"GEMINI_API_KEY": "k", "COMPANION_AGENT_THINKING_LEVEL": "medium"}, {"thinking_level": "medium"}),
            ({"GEMINI_API_KEY": "k", "COMPANION_AGENT_THINKING_LEVEL": ""}, {}),
            ({"OPENAI_API_KEY": "k"}, {}),
            ({"ANTHROPIC_API_KEY": "k"}, {}),
            ({}, {}),
        ]
        for env, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, {**base, **env}):
                os.environ.pop("COMPANION_AGENT_THINKING_LEVEL", None) if "COMPANION_AGENT_THINKING_LEVEL" not in env else None
                self.assertEqual(CompanionSpec().build_agent_config().provider_options, expected)

    def test_model_override(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "k",
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "COMPANION_AGENT_ANTHROPIC_MODEL": "claude-test",
        }
        with patch.dict(os.environ, env):
            llm = CompanionSpec().build_llm()
        self.assertIsInstance(llm, AnthropicProvider)
        self.assertEqual(llm.model, "claude-test")


class CompanionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_host_request_runs_on_the_companion_and_routes_to_an_agent(self) -> None:
        provider = DelegatingLLMProvider(
            final_text="routed", subagent_id="builder", subagent_prompt="scaffold"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ), patch.object(CompanionSpec, "build_llm", lambda self: provider):
            pool = _builder().host(Host(host_id="guide", agents=("pilot", "builder"))).build()
            pool.configure_runtime_database_url("postgresql://test/runtime")
            await pool.start()
            try:
                accepted = await pool.submit_host_request(
                    "guide", message="build it", session_id="s-1", user_id="u-1"
                )
                self.assertEqual(accepted["agent_id"], COMPANION_AGENT_ID)
                terminal = None
                async for event in pool.get_client(COMPANION_AGENT_ID).stream_response(
                    accepted["request_id"], timeout=10
                ):
                    if event.get("event") in {"request.completed", "request.error"}:
                        terminal = event
                        break
                self.assertEqual(terminal["event"], "request.completed", terminal)
                self.assertEqual(terminal["data"]["response"]["text"], "routed")
                self.assertIn("AGENTS\n", str(provider.last_system))
                runtime = pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
                [child] = await runtime.runtime_store.list_child_requests(accepted["request_id"])
                self.assertEqual(child["agent_id"], "builder")
                self.assertEqual(child["session_id"], child["request_id"])
            finally:
                await pool.close()


if __name__ == "__main__":
    unittest.main()


# --- workflows ---------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import httpx  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from mash.core.context import ToolCall  # noqa: E402
from mash.core.llm import LLMProvider  # noqa: E402
from mash.core.llm.types import LLMContentBlock, LLMResponse, LLMTokenUsage  # noqa: E402
from mash.runtime.agent_process import HostRelay  # noqa: E402
from mash.runtime.client import WorkflowInputError  # noqa: E402
from mash.runtime.factory import configure_run_workflow  # noqa: E402
from mash.runtime.host.tokens import derive_agent_token  # noqa: E402
from mash.testing.agents import DEFAULT_LOCAL_SECRET  # noqa: E402
from mash.tools.registry import ToolRegistry  # noqa: E402
from mash.workflows import CodeStep, StepContext, WorkflowSpec  # noqa: E402


class ChangelogIn(BaseModel):
    versions: int = Field(1, ge=1, le=10)


class ChangelogOut(BaseModel):
    summary: str


def _changelog(inp: ChangelogIn, ctx: StepContext) -> ChangelogOut:
    return ChangelogOut(summary=f"{inp.versions} releases")


def _workflow(workflow_id: str = "changelog") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        input_model=ChangelogIn,
        steps=[CodeStep(step_id="read", run=_changelog, input=ChangelogIn, output=ChangelogOut)],
        metadata={"display_name": "Changelog summary", "description": "Summarizes releases."},
    )


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    blocks = [LLMContentBlock.text(text)] if text else []
    blocks += [
        LLMContentBlock.tool_call(tool_call_id=c.id, name=c.name, arguments=c.arguments)
        for c in calls or []
    ]
    return LLMResponse(
        thought_summary="",
        text=text,
        tool_calls=list(calls or []),
        content_blocks=blocks,
        stop_reason="tool_call" if calls else "end_turn",
        usage=LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _WorkflowCaller(LLMProvider):
    def __init__(self) -> None:
        self.last_system = None
        self.tools: list[str] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request):
        self.last_system = request.system
        self.tools = [t.name for t in request.tools or []]
        if any(b.type == "tool_result" for m in request.messages for b in m.content):
            return _response("done")
        call = ToolCall(
            id="call-1", name="RunWorkflow",
            arguments={"workflow_id": "changelog", "input": {"versions": 3}},
        )
        return _response(calls=[call])

    def set_event_logger(self, logger, app_id: str) -> None:
        del logger, app_id


class CompanionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_the_snapshot_carries_the_hosts_own_workflows_not_the_pool_defaults(self) -> None:
        pool = (
            _builder()
            .enable_evals()
            .workflow(_workflow())
            .host(Host(host_id="guide", agents=("pilot",), workflows=("changelog",)))
            .build()
        )
        host = pool.get_host("guide")
        self.assertIn("run-experiment", host.workflows)
        self.assertEqual(pool.snapshot_for(host)["workflows"], ["changelog"])

    def test_run_workflow_is_registered_only_for_the_companion_with_workflows(self) -> None:
        access = SimpleNamespace(get_workflow_client=lambda: object())
        cases = [
            ("companion", {"agents": [], "workflows": ["changelog"]}, True),
            ("companion", {"agents": [], "workflows": []}, False),
            ("companion", None, False),
            ("pilot", {"agents": ["pilot"], "workflows": ["changelog"]}, False),
        ]
        for app_id, host, expected in cases:
            with self.subTest(app_id=app_id, host=host):
                runtime = SimpleNamespace(app_id=app_id, get_host_access=lambda: access)
                agent = SimpleNamespace(tools=ToolRegistry())
                configure_run_workflow(runtime, agent, host=host)
                self.assertEqual("RunWorkflow" in agent.tools, expected)

    async def test_the_companion_runs_a_workflow_through_the_relay(self) -> None:
        provider = _WorkflowCaller()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ), patch.object(CompanionSpec, "build_llm", lambda self: provider):
            pool = (
                _builder()
                .workflow(_workflow())
                .workflow(_workflow("other"))
                .host(Host(host_id="guide", agents=("pilot",), workflows=("changelog",)))
                .build()
            )
            pool.configure_runtime_database_url("postgresql://test/runtime")
            await pool.start()
            try:
                accepted = await pool.submit_host_request(
                    "guide", message="what changed in the last 3 releases?",
                    session_id="s-1", user_id="u-1",
                )
                request_id = accepted["request_id"]
                async for event in pool.get_client(COMPANION_AGENT_ID).stream_response(
                    request_id, timeout=15
                ):
                    if event.get("event") in {"request.completed", "request.error"}:
                        self.assertEqual(event["event"], "request.completed", event)
                        break

                system = str(provider.last_system)
                self.assertIn("WORKFLOWS", system)
                self.assertIn("- changelog | Changelog summary: Summarizes releases.", system)
                self.assertIn('"maximum":10', system)
                self.assertNotIn("- other |", system)
                self.assertIn("RunWorkflow", provider.tools)

                runtime = pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
                [turn] = await runtime.store.get_turns("s-1", COMPANION_AGENT_ID)
                [entry] = turn["metadata"]["ledger"]
                self.assertEqual(
                    (entry["workflow_id"], entry["status"], entry["companion_session_id"]),
                    ("changelog", "completed", "s-1"),
                )
                self.assertIn("3 releases", entry["got"])
                run = await pool.get_workflow_service().get_run("changelog", entry["run_id"])
                self.assertEqual(run.user_id, "u-1")
                self.assertTrue(run.dedup_key.startswith(f"{request_id}-wf-"))

                relay_url = await pool._agent_provider._ensure_relay()
                token = derive_agent_token(DEFAULT_LOCAL_SECRET, COMPANION_AGENT_ID)
                client = HostRelay(pool, host_api_url=relay_url, token=token).get_workflow_client()
                with self.assertRaises(WorkflowInputError) as refused:
                    await client.post_workflow_run(
                        "changelog", parent_request_id=request_id,
                        workflow_input={"versions": 50}, dedup_key=f"{request_id}-x",
                    )
                self.assertTrue(refused.exception.errors)

                async with httpx.AsyncClient(base_url=f"{relay_url}/h2a") as http:
                    def post(workflow_id, body, agent=COMPANION_AGENT_ID):
                        headers = {"Authorization": f"Bearer {derive_agent_token(DEFAULT_LOCAL_SECRET, agent)}"}
                        return http.post(f"/workflow/{workflow_id}/run", json=body, headers=headers)

                    parent = {"request_id": request_id}
                    outside = await post("other", {"parent": parent, "input": {}})
                    self.assertEqual(outside.status_code, 403)
                    bad_key = await post("changelog", {"parent": parent, "input": {}, "dedup_key": "elsewhere"})
                    self.assertEqual(bad_key.status_code, 400)
                    not_owner = await post("changelog", {"parent": parent, "input": {}}, agent="pilot")
                    self.assertEqual(not_owner.status_code, 403)

                    other_user = await pool.get_workflow_service().run_workflow(
                        "changelog", workflow_input={}, user_id="u-2"
                    )
                    read = await http.get(
                        f"/workflow/changelog/runs/{other_user.run_id}",
                        params={"parent_request_id": request_id},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(read.status_code, 403)
                    own = await http.get(
                        f"/workflow/changelog/runs/{entry['run_id']}",
                        params={"parent_request_id": request_id},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(own.json()["status"], "completed")
            finally:
                await pool.close()

"""The companion: the built-in router of every host request."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mash.agents.companion import COMPANION_AGENT_ID, CompanionSpec, build_companion_metadata
from mash.core.llm import AnthropicProvider
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


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
        self.assertEqual(config.agent_id, "companion")
        self.assertEqual(config.compaction_token_threshold, 100_000)
        self.assertFalse(spec.enable_sandbox())
        self.assertEqual(spec.browser_sites(), [])
        self.assertEqual(spec.build_skills().list_skills(), [])
        self.assertIn("about", config.system_prompt)
        self.assertIn("unclear", config.system_prompt)
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


# --- workflows ---------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402

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


class CompanionWorkflowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

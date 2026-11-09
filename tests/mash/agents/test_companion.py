"""The companion: the built-in primary of a host that names none."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from mash.agents.companion import COMPANION_AGENT_ID, CompanionSpec, build_companion_metadata
from mash.core.llm import AnthropicProvider
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import DelegatingLLMProvider, build_spec, metadata


def _builder(*, companion: bool = True) -> HostBuilder:
    builder = (
        HostBuilder()
        .agent(build_spec(agent_id="pilot"), metadata=metadata())
        .agent(build_spec(agent_id="builder"), metadata=metadata())
    )
    return builder.enable_companion() if companion else builder


class HostWithoutPrimaryTests(unittest.TestCase):
    def test_a_host_without_a_primary_gets_the_companion(self) -> None:
        pool = _builder().host(Host(host_id="guide", subagents=("pilot", "builder"))).build()

        host = pool.get_host("guide")
        self.assertEqual(host.primary, COMPANION_AGENT_ID)
        self.assertEqual(host.subagents, ("pilot", "builder"))
        self.assertEqual(pool.snapshot_for(host)["primary"], COMPANION_AGENT_ID)
        self.assertIn(COMPANION_AGENT_ID, pool.list_registered_agents())

    def test_without_enable_companion_a_host_needs_a_primary(self) -> None:
        with self.assertRaisesRegex(ValueError, "enable_companion"):
            _builder(companion=False).host(Host(host_id="guide", subagents=("pilot",))).build()

    def test_a_host_that_names_a_primary_is_unchanged(self) -> None:
        pool = (
            _builder()
            .host(Host(host_id="baseline", primary="pilot", subagents=("builder",)))
            .build()
        )
        self.assertEqual(pool.get_host("baseline").primary, "pilot")

    def test_define_host_on_a_running_pool_fills_in_the_companion(self) -> None:
        pool = _builder().build()
        defined = pool.define_host(Host(host_id="later", subagents=("builder",)))
        self.assertEqual(defined.primary, COMPANION_AGENT_ID)

    def test_the_companion_cannot_also_be_a_subagent(self) -> None:
        with self.assertRaises(ValueError):
            _builder().host(Host(host_id="g", subagents=(COMPANION_AGENT_ID,))).build()

    def test_an_empty_primary_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            Host(host_id="g", primary=" ")


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
        self.assertIn("Never send an item\n  to yourself", config.system_prompt)
        self.assertIn("session_id", config.system_prompt)
        self.assertTrue(build_companion_metadata().description)

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
    async def test_a_host_request_runs_on_the_companion_and_routes_to_a_subagent(self) -> None:
        provider = DelegatingLLMProvider(
            final_text="routed", subagent_id="builder", subagent_prompt="scaffold"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ), patch.object(CompanionSpec, "build_llm", lambda self: provider):
            pool = _builder().host(Host(host_id="guide", subagents=("pilot", "builder"))).build()
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
                self.assertIn("SUBAGENTS", str(provider.last_system))
                runtime = pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
                [child] = await runtime.runtime_store.list_child_requests(accepted["request_id"])
                self.assertEqual(child["agent_id"], "builder")
                self.assertEqual(child["session_id"], child["request_id"])
            finally:
                await pool.close()


if __name__ == "__main__":
    unittest.main()

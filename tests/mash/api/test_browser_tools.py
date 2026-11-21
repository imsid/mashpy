"""Browser tools end to end: a pool, its agent process stand-in, the relay, a browser."""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from mash.browser.manager import BrowserManager
from mash.logging.trace_context import bound_workflow_ids
from mash.runtime import AgentRuntime, HostBuilder
from mash.runtime.engine.steps import start_browser_handoff
from mash.sandbox.manager import SandboxManager
from mash.testing.browsers import FakePage, LocalBrowserProvider
from mash.testing.runtime_fixtures import DEFAULT_POLICY, TEST_IDENTITY, build_spec, build_tool_calling_spec, metadata
from mash.testing.sandboxes import LocalSandboxProvider
from mash.tools.browser import browser_tool_names

PAGES = {"https://www.pge.com/": FakePage(title="PG&E account")}


class BrowserToolsEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        sites=("https://*.pge.com",),
        answer: str = "done",
    ) -> tuple[str, list[dict[str, Any]], LocalBrowserProvider]:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": ""}
        ):
            spec = build_tool_calling_spec(
                agent_id="billing", tool_name=tool_name, arguments=arguments
            )
            spec.browser_sites = lambda: list(sites)
            pool = HostBuilder().agent(spec, metadata=metadata()).build()
            provider = LocalBrowserProvider(pages=PAGES)
            pool.configure_runtime_database_url("postgresql://test/runtime")
            pool.configure_sandboxes(SandboxManager(LocalSandboxProvider()))
            pool.configure_browsers(BrowserManager(provider, host_secret="local-agents"))
            await pool.start()
            interactions: list[dict[str, Any]] = []
            try:
                client = pool.get_client("billing")
                request_id = await client.post_request("go", session_id="s-1", **TEST_IDENTITY)
                events = []
                async for event in client.stream_response(request_id, timeout=20):
                    events.append(event)
                    if event["event"] == "request.interaction.create":
                        payload = event["data"]
                        # The interaction carries no link: one is minted when
                        # someone asks, as the events API does.
                        handoff = pool.get_browsers().mint_link(
                            "p-1", request_id, payload["interaction_id"]
                        )
                        relay = await pool._agent_provider._ensure_relay()
                        url = f"{relay}/s/{handoff.token}/browser/"
                        async with httpx.AsyncClient() as http:
                            view = await http.get(url, follow_redirects=False)
                        interactions.append(
                            {**payload, "view_status": view.status_code, "link": url}
                        )
                        await client.post_interaction(
                            request_id,
                            interaction_id=payload["interaction_id"],
                            response=answer,
                        )
                    if event["event"] == "request.completed" and interactions:
                        # Closing the interaction revoked the link.
                        async with httpx.AsyncClient() as http:
                            after = await http.get(interactions[-1]["link"], follow_redirects=False)
                        interactions[-1]["view_status_after"] = after.status_code
            finally:
                await pool.close()
                await provider.close()
        self.assertEqual(events[-1]["event"], "request.completed", events[-1])
        return events[-1]["data"]["response"]["text"], interactions, provider

    async def test_an_agent_with_sites_browses_in_the_request_users_browser(self) -> None:
        text, _, provider = await self._run(
            "browser_navigate", {"url": "https://www.pge.com/"}
        )

        self.assertIn("<web-content>", text)
        self.assertIn("PG&E account", text)
        self.assertEqual(list(provider.browsers), ["p-1"])

    async def test_the_sites_hold_for_the_model(self) -> None:
        text, _, _ = await self._run("browser_navigate", {"url": "https://evil.io/"})

        self.assertIn("https://evil.io is outside the sites", text)

    async def test_handoff_pauses_on_a_browser_interaction_whose_links_are_minted_on_demand(self) -> None:
        text, interactions, _ = await self._run(
            "browser_handoff", {"reason": "Sign in to pge.com"}
        )

        [interaction] = interactions
        self.assertEqual(interaction["type"], "browser")
        self.assertEqual(interaction["prompt"], "Sign in to pge.com")
        self.assertEqual(interaction["schema"], {"type": "enum", "options": ["done", "cancel"]})
        self.assertNotIn("url", interaction)
        # No timeout of its own: the policy's cap.
        self.assertEqual(interaction["timeout_seconds"], 86400)
        self.assertEqual(interaction["view_status"], 307)
        self.assertEqual(interaction["view_status_after"], 404)
        self.assertTrue(text.startswith("The user is done in the browser. The page now:"))

    async def test_a_cancelled_handoff_tells_the_model(self) -> None:
        text, _, _ = await self._run(
            "browser_handoff", {"reason": "Sign in"}, answer="cancel"
        )

        self.assertEqual(text, "The user cancelled the handoff.")


class BrowserToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def _runtime(self, spec):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name}):
            from conftest import build_test_stores

            runtime_store, memory_store = build_test_stores()
            runtime = AgentRuntime.from_spec(
                spec, session_id="s-1", runtime_store=runtime_store, memory_store=memory_store
            )
        return runtime

    async def test_the_relayed_tools_and_mashs_three_are_registered(self) -> None:
        spec = build_spec(agent_id="billing")
        runtime = await self._runtime(spec)
        runtime.enable_browser_tools(host_api_url="http://host", token="t")

        for name in browser_tool_names():
            self.assertIn(name, runtime.agent.tools)
        self.assertNotIn("browser_evaluate", runtime.agent.tools)

    def _turn_tools(self, runtime, policy):
        return runtime.build_turn_agent(
            session_id="s-1", trace_id="t-1", app_id="app-1", policy=policy
        ).tools

    async def test_the_policy_narrows_a_turns_sandbox_and_browser_tools(self) -> None:
        from mash.tools.sandbox import SANDBOX_TOOL_NAMES

        spec = build_spec(agent_id="billing")
        spec.browser_sites = lambda: ["https://*.pge.com"]
        runtime = await self._runtime(spec)
        runtime.enable_sandbox_tools(host_api_url="http://host", token="t")
        runtime.enable_browser_tools(host_api_url="http://host", token="t")
        names = runtime.agent.tools.list_tools()

        full = self._turn_tools(runtime, DEFAULT_POLICY)
        # The base agent's tools, then the ones bound to the request.
        self.assertEqual(full.list_tools()[: len(names)], names)
        self.assertIn("https://*.pge.com", full.get("browser_navigate").description)

        dry = self._turn_tools(runtime, {**DEFAULT_POLICY, "sandbox": False})
        for name in (*SANDBOX_TOOL_NAMES, "browser_save", "browser_upload"):
            self.assertNotIn(name, dry)
        self.assertIn("browser_navigate", dry)

        narrow = self._turn_tools(runtime, {**DEFAULT_POLICY, "browser_sites": ["https://m.pge.com"]})
        description = narrow.get("browser_navigate").description
        self.assertIn("https://m.pge.com", description)
        self.assertNotIn("*.pge.com", description)
        # The listed copy keeps its place in the tool list.
        self.assertEqual(narrow.list_tools()[: len(names)], names)

        for sites in ([], ["https://evil.io"]):
            closed = self._turn_tools(runtime, {**DEFAULT_POLICY, "browser_sites": sites})
            for name in browser_tool_names():
                self.assertNotIn(name, closed)
            self.assertIn("run_code", closed)

        # The base agent's tools are untouched.
        self.assertEqual(runtime.agent.tools.list_tools(), names)
        self.assertNotIn("m.pge.com", runtime.agent.tools.get("browser_navigate").description)

    async def test_a_spec_with_its_own_browser_tool_fails(self) -> None:
        from mash.tools.base import FunctionTool, ToolResult

        spec = build_spec(agent_id="billing")
        tools = spec.build_tools()

        async def _noop(args):
            return ToolResult.success("")

        tools.register(
            FunctionTool(
                name="browser_navigate", description="x",
                parameters={"type": "object", "properties": {}}, _executor=_noop,
            )
        )
        spec.build_tools = lambda: tools
        runtime = await self._runtime(spec)

        with self.assertRaisesRegex(ValueError, "registers its own 'browser_navigate'"):
            runtime.enable_browser_tools(host_api_url="http://host", token="t")

    async def test_bad_sites_fail_when_the_agent_starts(self) -> None:
        spec = build_spec(agent_id="billing")
        spec.browser_sites = lambda: ["pge.com"]
        runtime = await self._runtime(spec)

        with self.assertRaisesRegex(ValueError, "agent 'billing': browser site 'pge.com'"):
            runtime.enable_browser_tools(host_api_url="http://host", token="t")

    async def test_handoff_fails_at_once_in_a_workflow_request(self) -> None:
        runtime = await self._runtime(build_spec(agent_id="billing"))
        runtime.enable_browser_tools(host_api_url="http://unreachable.invalid", token="t")

        with bound_workflow_ids("wf", "run-1"):
            started = await start_browser_handoff(runtime, "req-1")

        self.assertIn("Nobody is watching this workflow run", started["error"])


if __name__ == "__main__":
    unittest.main()

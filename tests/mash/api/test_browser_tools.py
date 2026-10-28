"""Browser tools end to end: a pool, its agent process stand-in, the relay, a browser."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from collections import defaultdict
from typing import Any
from unittest.mock import patch

import httpx

from mash.browser.manager import BrowserManager
from mash.logging.trace_context import bound_workflow_ids
from mash.runtime import AgentRuntime, HostBuilder
from mash.runtime.engine.dbos import register_runtime, unregister_runtime
from mash.runtime.engine.steps import start_browser_handoff
from mash.sandbox.manager import SandboxManager
from mash.testing.browsers import FakePage, LocalBrowserProvider
from mash.testing.runtime_fixtures import build_spec, build_tool_calling_spec, metadata
from mash.testing.sandboxes import LocalSandboxProvider
from mash.tools.browser import browser_tool_names

PAGES = {"https://www.pge.com/": FakePage(title="PG&E account")}


class _InlineDBOS:
    """Just enough DBOS for a parked interaction: steps run inline, send/recv by topic."""

    def __init__(self) -> None:
        self.topics: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)

    async def run_step_async(self, config, fn, *args, **kwargs):
        del config
        return await fn(*args, **kwargs)

    async def recv_async(self, topic, timeout_seconds=None):
        try:
            return await asyncio.wait_for(self.topics[topic].get(), timeout_seconds)
        except asyncio.TimeoutError:
            return None

    async def send_async(self, workflow_id, value, topic=None):
        del workflow_id
        await self.topics[topic].put(value)


@contextlib.contextmanager
def _durable_handoffs():
    """Route browser_handoff through the engine's interception in the test executor.

    The test suite runs requests inline, without DBOS; this swaps in the
    engine's handoff handler for that one tool, over ``_InlineDBOS``.
    """
    import conftest
    from mash.runtime.engine import workflow as engine_workflow

    fake = _InlineDBOS()
    original = conftest.run_step_tool_call

    async def run_step_tool_call(agent_id, request_id, session_id, trace_id, state, payload):
        if payload["name"] == "browser_handoff":
            return await engine_workflow._handle_browser_handoff(
                agent_id, request_id, session_id, trace_id, state,
                tool_call=payload, loop_index=int(state.get("loop_index") or 0), call_index=0,
            )
        return await original(agent_id, request_id, session_id, trace_id, state, payload)

    with patch.object(conftest, "run_step_tool_call", run_step_tool_call), patch.object(
        engine_workflow, "DBOS", fake
    ), patch("mash.runtime.server.DBOS", fake):
        yield


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
        ), _durable_handoffs():
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
                request_id = await client.post_request("go", session_id="s-1", user_id="u-1")
                events = []
                async for event in client.stream_response(request_id, timeout=20):
                    events.append(event)
                    if event["event"] == "request.interaction.create":
                        payload = event["data"]
                        async with httpx.AsyncClient() as http:
                            view = await http.get(payload["url"], follow_redirects=False)
                        interactions.append({**payload, "view_status": view.status_code})
                        await client.post_interaction(
                            request_id,
                            interaction_id=payload["interaction_id"],
                            response=answer,
                        )
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
        self.assertEqual(list(provider.browsers), ["u-1"])

    async def test_the_sites_hold_for_the_model(self) -> None:
        text, _, _ = await self._run("browser_navigate", {"url": "https://evil.io/"})

        self.assertIn("https://evil.io is outside the sites", text)

    async def test_handoff_pauses_on_a_browser_interaction_with_a_live_link(self) -> None:
        text, interactions, _ = await self._run(
            "browser_handoff", {"reason": "Sign in to pge.com"}
        )

        [interaction] = interactions
        self.assertEqual(interaction["type"], "browser")
        self.assertEqual(interaction["prompt"], "Sign in to pge.com")
        self.assertEqual(interaction["schema"], {"type": "enum", "options": ["done", "cancel"]})
        self.assertRegex(interaction["url"], r"^http://127\.0\.0\.1:\d+/s/[^/]+/browser/$")
        self.assertIsInstance(interaction["expires_at"], int)
        self.assertEqual(interaction["view_status"], 307)
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
        register_runtime(runtime)
        self.addCleanup(unregister_runtime, runtime)

        with bound_workflow_ids("wf", "run-1"):
            started = await start_browser_handoff("billing", "req-1")

        self.assertIn("Nobody is watching this workflow run", started["error"])


if __name__ == "__main__":
    unittest.main()

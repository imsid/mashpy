"""MCP and web search calls must not block the agent's event loop.

Each test runs a real HTTP MCP server in a thread and drives the runtime the
way a request does: open it, build the turn agent, run the tool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.mcp.types import MCPServerConfig
from mash.runtime import AgentRuntime
from mash.runtime.spec import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry
from mash.tools.web_search import ParallelSearchProvider

from tests.mash.mcp.fake_server import FakeMCPServer, unreachable_url


class _LLM(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def set_event_logger(self, logger, app_id: str) -> None:
        return None


class _Spec(AgentSpec):
    def __init__(
        self,
        servers: list[MCPServerConfig],
        *,
        web_search: ParallelSearchProvider | None = None,
    ) -> None:
        self._servers = servers
        self._web_search = web_search

    def get_agent_id(self) -> str:
        return "mcp-test"

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _LLM()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id="mcp-test", system_prompt="test")

    def build_mcp_servers(self) -> list[MCPServerConfig]:
        return self._servers

    def build_web_search(self):
        return self._web_search


async def _ticks_while(awaitable) -> tuple[object, int]:
    """Run ``awaitable`` and count how often another coroutine got to run."""
    ticks = 0
    done = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.05)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await awaitable
    finally:
        done.set()
        await ticker
    return result, ticks


class MCPBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"MASH_DATA_DIR": self._tmp.name})
        self._env.start()
        self._runtimes: list[AgentRuntime] = []

    async def asyncTearDown(self) -> None:
        for runtime in self._runtimes:
            await runtime.shutdown()
        self._env.stop()
        self._tmp.cleanup()

    async def _open(self, spec: _Spec) -> AgentRuntime:
        runtime_store, memory_store = build_test_stores()
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="s-1",
            runtime_store=runtime_store,
            memory_store=memory_store,
        )
        self._runtimes.append(runtime)
        await runtime.open()
        return runtime

    def _turn_agent(self, runtime: AgentRuntime):
        return runtime.build_turn_agent(session_id="s-1", trace_id="t-1", user_id="u-1")

    async def test_slow_tool_call_lets_other_coroutines_run(self) -> None:
        with FakeMCPServer() as server:
            runtime = await self._open(_Spec([MCPServerConfig(name="fake", url=server.url)]))
            tool = self._turn_agent(runtime).tools.get("mcp_fake_slow_echo")

            result, ticks = await _ticks_while(tool.execute({"text": "hi", "seconds": 1}))

        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.content, "hi")
        self.assertGreaterEqual(ticks, 10)

    async def test_parallel_calls_overlap(self) -> None:
        with FakeMCPServer() as server:
            runtime = await self._open(_Spec([MCPServerConfig(name="fake", url=server.url)]))
            agent = self._turn_agent(runtime)
            tool = agent.tools.get("mcp_fake_slow_echo")

            started = time.monotonic()
            results = await asyncio.gather(
                tool.execute({"text": "a", "seconds": 1}),
                tool.execute({"text": "b", "seconds": 1}),
            )
            elapsed = time.monotonic() - started

        self.assertEqual([r.content for r in results], ["a", "b"])
        self.assertLess(elapsed, 1.6)

    async def test_tools_are_listed_once_per_server(self) -> None:
        with FakeMCPServer() as mcp, FakeMCPServer() as search:
            spec = _Spec(
                [MCPServerConfig(name="fake", url=mcp.url)],
                web_search=ParallelSearchProvider(
                    api_key="k", base_url=search.url, allowed_tools=["slow_echo"]
                ),
            )
            runtime = await self._open(spec)
            for _ in range(3):
                agent = self._turn_agent(runtime)
                self.assertIn("mcp_fake_slow_echo", agent.tools)
                self.assertIn("slow_echo", agent.tools)

            self.assertEqual(mcp.calls["tools/list"], 1)
            self.assertEqual(search.calls["tools/list"], 1)

    async def test_unreachable_server_is_skipped_and_logged(self) -> None:
        with FakeMCPServer() as server:
            spec = _Spec(
                [
                    MCPServerConfig(name="down", url=unreachable_url()),
                    MCPServerConfig(name="up", url=server.url),
                ],
                web_search=ParallelSearchProvider(
                    api_key="k", base_url=unreachable_url(), allowed_tools=["slow_echo"]
                ),
            )
            with self.assertLogs("mash.mcp", level="WARNING") as logs:
                runtime = await self._open(spec)

            names = set(runtime.tools.list_tools())
            self.assertIn("mcp_up_slow_echo", names)
            self.assertNotIn("slow_echo", names)
            self.assertIn("mcp_up_slow_echo", self._turn_agent(runtime).tools)

        output = "\n".join(logs.output)
        self.assertIn("'down'", output)
        self.assertIn("'parallel_web_search'", output)

    async def test_tool_arguments_stay_out_of_logs(self) -> None:
        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect(level=logging.DEBUG)
        mash_logger = logging.getLogger("mash")
        previous = mash_logger.level
        mash_logger.addHandler(handler)
        mash_logger.setLevel(logging.DEBUG)
        try:
            with FakeMCPServer() as server:
                runtime = await self._open(
                    _Spec([MCPServerConfig(name="fake", url=server.url)])
                )
                tool = self._turn_agent(runtime).tools.get("mcp_fake_slow_echo")
                result = await tool.execute({"text": "s3cr3t-value", "seconds": 0})
        finally:
            mash_logger.removeHandler(handler)
            mash_logger.setLevel(previous)

        self.assertEqual(result.content, "s3cr3t-value")
        messages = [record.getMessage() for record in records]
        self.assertTrue(any("tools/call" in message for message in messages), messages)
        self.assertFalse(any("s3cr3t-value" in message for message in messages))

    async def test_session_server_opens_and_shuts_down(self) -> None:
        with FakeMCPServer(session_id="sess-1") as server:
            runtime = await self._open(_Spec([MCPServerConfig(name="fake", url=server.url)]))
            tool = self._turn_agent(runtime).tools.get("mcp_fake_slow_echo")
            result = await tool.execute({"text": "ok", "seconds": 0})
            self.assertEqual(result.content, "ok")
            started = time.monotonic()
            await runtime.shutdown()
            self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()

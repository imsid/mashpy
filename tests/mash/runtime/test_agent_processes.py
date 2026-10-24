"""AgentProcessProvider with real ``mash agent serve`` children.

The children run their own runtime and DBOS against a real Postgres, so these
tests skip when one is not reachable (``MASH_REAL_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import os
import signal
import unittest
import uuid

from mash.runtime.client import AgentClient
from mash.runtime.host.processes import STATUS_RUNNING, AgentProcessProvider

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency missing
    psycopg = None

HOST_APP = "tests.mash.runtime.process_pool:build_pool"


def _database_url() -> str:
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


class AgentProcessProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent_id = f"proc-{uuid.uuid4().hex[:8]}"
        self.environ = {
            **os.environ,
            "MASH_DATABASE_URL": _database_url(),
            "TEST_PROCESS_AGENT_ID": self.agent_id,
            "TEST_PROCESS_DELAY": "3",
        }
        self.provider = AgentProcessProvider(
            host_app=HOST_APP,
            host_secret="secret",
            host_api_url="http://127.0.0.1:1",
            environ=self.environ,
            health_timeout=90,
        )

    async def asyncTearDown(self) -> None:
        await self.provider.close()

    async def _kill_and_wait_for_restart(self) -> None:
        os.kill(self.provider._children[self.agent_id].process.pid, signal.SIGKILL)
        await self._wait_for(lambda status: status != STATUS_RUNNING)
        await self._wait_for(lambda status: status == STATUS_RUNNING)

    async def _wait_for(self, condition) -> None:
        for _ in range(900):
            if condition(self.provider.status(self.agent_id)):
                return
            await asyncio.sleep(0.1)
        self.fail(f"agent status stayed {self.provider.status(self.agent_id)}")

    async def test_spawns_a_child_and_restarts_it_on_the_same_port(self) -> None:
        endpoint = await self.provider.ensure(self.agent_id)
        client = AgentClient(endpoint.base_url, self.agent_id, token=endpoint.token)
        self.assertEqual((await client.health())["agent_id"], self.agent_id)

        await self._kill_and_wait_for_restart()

        self.assertEqual(await self.provider.ensure(self.agent_id), endpoint)
        self.assertEqual((await client.health())["agent_id"], self.agent_id)

    async def test_a_pending_request_finishes_after_the_child_is_killed(self) -> None:
        endpoint = await self.provider.ensure(self.agent_id)
        client = AgentClient(endpoint.base_url, self.agent_id, token=endpoint.token)
        request_id = await client.post_request("hello", session_id="s-1", user_id="u-1")
        await asyncio.sleep(1.0)  # the model call is in flight

        await self._kill_and_wait_for_restart()

        events = [event async for event in client.stream_response(request_id, timeout=60)]
        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(events[-1]["data"]["response"]["text"], "process-ok")

    async def test_stop_ends_the_child(self) -> None:
        await self.provider.ensure(self.agent_id)
        process = self.provider._children[self.agent_id].process

        await self.provider.stop(self.agent_id)

        self.assertIsNotNone(process.returncode)
        self.assertIsNone(self.provider.status(self.agent_id))


if __name__ == "__main__":
    unittest.main()


class EnableDisableTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_request_in_flight_finishes_after_the_agent_is_enabled_again(self) -> None:
        from unittest.mock import patch

        from mash.runtime.host.host import AgentDisabledError
        from tests.mash.runtime.process_pool import build_pool

        agent_id = f"proc-{uuid.uuid4().hex[:8]}"
        environ = {
            **os.environ,
            "MASH_DATABASE_URL": _database_url(),
            "TEST_PROCESS_AGENT_ID": agent_id,
            "TEST_PROCESS_DELAY": "3",
        }
        with patch.dict(os.environ, {"TEST_PROCESS_AGENT_ID": agent_id}):
            pool = build_pool()
        pool.configure_runtime_database_url("postgresql://test/runtime")
        pool.configure_agent_provider(
            AgentProcessProvider(
                host_app=HOST_APP,
                host_secret="secret",
                host_api_url="http://127.0.0.1:1",
                environ=environ,
                health_timeout=90,
                stop_timeout=3,
            )
        )
        await pool.start()
        try:
            request_id = await pool.get_client(agent_id).post_request(
                "hello", session_id="s-1", user_id="u-1"
            )
            await asyncio.sleep(1.0)  # the model call is in flight

            await pool.disable_agent(agent_id)
            self.assertEqual(pool.agent_status(agent_id), "disabled")
            with self.assertRaises(AgentDisabledError):
                pool.get_client(agent_id)

            await pool.enable_agent(agent_id)
            client = pool.get_client(agent_id)
            events = [event async for event in client.stream_response(request_id, timeout=60)]
        finally:
            await pool.close()

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(events[-1]["data"]["response"]["text"], "process-ok")

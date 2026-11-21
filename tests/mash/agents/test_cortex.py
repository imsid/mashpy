"""Cortex reads: settings, the profile block, ``search_cortex``, and wiring in an agent process."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import patch

from conftest import build_test_stores
from mash.agents.cortex import (
    CortexEntry,
    CortexReader,
    CortexSettings,
    CortexStore,
    profile_block,
)
from mash.runtime import HostBuilder
from mash.runtime.agent_process import build_agent_server
from mash.runtime.factory import build_turn_agent
from mash.testing.runtime_fixtures import DEFAULT_POLICY, build_spec, metadata

TEST_URL = "postgresql://test/runtime"

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _entry(entry_id: str, app_id: str, topic: str, content: str, updated_at: float) -> CortexEntry:
    return CortexEntry(
        entry_id=entry_id,
        app_id=app_id,
        topic=topic,
        content=content,
        origin="build",
        sources=[{"agent_id": "assistant", "session_id": "s-1", "trace_id": "t-1"}],
        created_at=updated_at,
        updated_at=updated_at,
    )


def _reader() -> CortexReader:
    store = CortexStore(TEST_URL)
    store._mem.profiles["app-1"] = "Prefers metric units. Works at Acme."
    store._mem.entries["e-1"] = _entry("e-1", "app-1", "units", "Prefers metric units", 2.0)
    store._mem.entries["e-2"] = _entry("e-2", "app-2", "units", "Prefers imperial units", 3.0)
    return CortexReader(store, CortexSettings())


def _pool(*, cortex: bool):
    builder = HostBuilder().agent(build_spec(agent_id="assistant"), metadata=metadata())
    if cortex:
        builder = builder.enable_cortex(profile_max_chars=200)
    return builder.build()


class SettingsTests(unittest.TestCase):
    def test_enable_cortex_sets_pool_settings(self) -> None:
        self.assertIsNone(_pool(cortex=False).cortex)
        self.assertEqual(_pool(cortex=True).cortex, CortexSettings(profile_max_chars=200))

    def test_profile_max_chars_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            CortexSettings(profile_max_chars=0)


class ProfileBlockTests(unittest.TestCase):
    def test_empty_profile_adds_nothing(self) -> None:
        self.assertEqual(profile_block("  ", 100), "")

    def test_block_carries_the_profile(self) -> None:
        block = profile_block("Prefers metric units.", 100)

        self.assertTrue(block.startswith("APP MEMORY"))
        self.assertIn("Prefers metric units.", block)
        self.assertIn("search_cortex", block)

    def test_long_profile_is_truncated_with_a_marker(self) -> None:
        block = profile_block("x" * 50, 10)

        self.assertIn("x" * 10 + "\n[profile truncated]", block)
        self.assertNotIn("x" * 11, block)


class SearchCortexToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_the_request_users_entries(self) -> None:
        tool = _reader().search_tool("app-1")

        # A app_id in the arguments is ignored; the scope comes from the request.
        result = await tool.execute({"query": "units", "app_id": "app-2"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual([e["content"] for e in payload["entries"]], ["Prefers metric units"])
        self.assertEqual(payload["entries"][0]["sources"][0]["agent_id"], "assistant")
        self.assertNotIn("app-1", result.content)

    async def test_blank_query_is_an_error(self) -> None:
        result = await _reader().search_tool("app-1").execute({"query": "  "})

        self.assertTrue(result.is_error)

    async def test_profile_block_for_a_user_without_cortex_is_empty(self) -> None:
        reader = _reader()

        self.assertIn("Works at Acme", await reader.profile_block("app-1"))
        self.assertEqual(await reader.profile_block("u-unknown"), "")


class AgentProcessWiringTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, *, cortex: bool):
        return build_agent_server(
            _pool(cortex=cortex), "assistant", token="tok", host_api_url="http://host"
        )

    async def test_search_cortex_only_with_cortex_and_a_user(self) -> None:
        enabled = self._server(cortex=True).runtime
        disabled = self._server(cortex=False).runtime

        def tools(runtime, app_id):
            return build_turn_agent(runtime, session_id="s-1", app_id=app_id).tools

        self.assertIn("search_cortex", tools(enabled, "app-1"))
        self.assertNotIn("search_cortex", tools(enabled, None))
        self.assertNotIn("search_cortex", tools(disabled, "app-1"))

    async def test_request_sees_its_users_profile(self) -> None:
        runtime_store, memory_store = build_test_stores()
        p = patch("mash.runtime.spec.PostgresStore", lambda _url: memory_store)
        p.start()
        self.addCleanup(p.stop)
        server = self._server(cortex=True)
        runtime = server.runtime
        runtime.get_cortex().store._mem.profiles["app-1"] = "Prefers metric units."
        provider = runtime.definition.provider

        await runtime.open()
        try:
            systems = {}
            for app_id in ("app-1", "app-2"):
                accepted = await runtime.submit_request(
                    message="hi",
                    session_id=f"s-{uuid.uuid4()}",
                    app_id=app_id,
                    policy_id="p-1",
                    policy=DEFAULT_POLICY,
                )
                for _ in range(200):
                    if await runtime.runtime_store.is_request_terminal(accepted["request_id"]):
                        break
                    await asyncio.sleep(0.01)
                systems[app_id] = str(provider.last_system)
        finally:
            await runtime.shutdown()

        self.assertIn("Prefers metric units.", systems["app-1"])
        self.assertNotIn("APP MEMORY", systems["app-2"])


class PostgresCortexStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        self.database_url = os.environ.get(
            "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
        )
        try:
            with psycopg.connect(self.database_url, autocommit=True) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on local postgres
            raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
        self.store = CortexStore(self.database_url)
        await self.store.open()
        self.app_id = f"cortex-{uuid.uuid4().hex}"
        self.other_app_id = f"{self.app_id}-other"
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            for app_id, profile in ((self.app_id, "Works at Acme."), (self.other_app_id, "")):
                conn.execute(
                    "INSERT INTO cortex_app (app_id, profile, updated_at) VALUES (%s, %s, 1)",
                    (app_id, profile),
                )
            for entry_id, app_id, content, updated_at in (
                ("a", self.app_id, "Prefers metric units", 1.0),
                ("b", self.app_id, "Ships on Fridays", 2.0),
                ("c", self.other_app_id, "Prefers imperial units", 3.0),
            ):
                conn.execute(
                    """
                    INSERT INTO cortex_entry
                        (entry_id, app_id, topic, content, origin, created_at, updated_at)
                    VALUES (%s, %s, 'prefs', %s, 'build', %s, %s)
                    """,
                    (f"{app_id}-{entry_id}", app_id, content, updated_at, updated_at),
                )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM cortex_app WHERE app_id IN (%s, %s)",
                (self.app_id, self.other_app_id),
            )

    async def test_profile_and_search_are_per_user(self) -> None:
        self.assertEqual(await self.store.get_profile(self.app_id), "Works at Acme.")
        self.assertIsNone(await self.store.get_profile(f"{self.app_id}-missing"))

        hits = await self.store.search_entries(self.app_id, "units")
        self.assertEqual([hit.content for hit in hits], ["Prefers metric units"])
        self.assertEqual(hits[0].origin, "build")
        self.assertEqual(await self.store.search_entries(self.app_id, "   "), [])


if __name__ == "__main__":
    unittest.main()

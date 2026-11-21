"""PolicyStore against a real Postgres."""

from __future__ import annotations

import os
import unittest
import uuid

from mash.policies import Policy, PolicyStore

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _database_url() -> str:
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash",
    )
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


class PolicyStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = _database_url()
        self.policy_id = f"policy-{uuid.uuid4()}"
        self.store = PolicyStore(self.database_url)
        await self.store.open()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("DELETE FROM policies WHERE policy_id = %s", (self.policy_id,))

    async def test_get_of_an_unknown_policy_is_none(self) -> None:
        self.assertIsNone(await self.store.get(self.policy_id))

    async def test_put_creates_then_replaces_and_keeps_created_at(self) -> None:
        first = await self.store.put(
            self.policy_id,
            Policy(sandbox=False, browser_sites=["https://*.pge.com"], wait_max_seconds=None),
        )
        self.assertEqual(
            first.policy,
            Policy(sandbox=False, browser_sites=["https://*.pge.com"], wait_max_seconds=None),
        )
        self.assertEqual(first.created_at, first.updated_at)

        second = await self.store.put(self.policy_id, Policy())
        self.assertEqual(second.policy, Policy())
        self.assertEqual(second.created_at, first.created_at)
        self.assertGreaterEqual(second.updated_at, first.updated_at)
        self.assertEqual(await self.store.get(self.policy_id), second)

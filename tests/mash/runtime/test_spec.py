"""Tests for AgentSpec defaults."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mash.memory.store import PostgresStore
from mash.testing.runtime_fixtures import build_spec


class AgentSpecMemoryStoreTests(unittest.TestCase):
    def test_build_memory_store_raises_without_database_url(self) -> None:
        with patch.dict("os.environ", {"MASH_DATABASE_URL": ""}, clear=False):
            spec = build_spec(agent_id="primary", response_text="ok")
            with self.assertRaises(RuntimeError):
                spec.build_memory_store()

    def test_build_memory_store_uses_postgres_when_database_url_is_set(
        self,
    ) -> None:
        with patch.dict(
            "os.environ",
            {"MASH_DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:5432/mash"},
            clear=False,
        ):
            spec = build_spec(agent_id="primary", response_text="ok")
            store = spec.build_memory_store()
            self.assertIsInstance(store, PostgresStore)


if __name__ == "__main__":
    unittest.main()

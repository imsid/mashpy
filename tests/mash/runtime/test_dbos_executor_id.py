"""Tests for the per-process DBOS executor id."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from mash.runtime.engine import dbos as engine_dbos


class _FakeDBOS:
    configs: list[dict[str, Any]] = []
    listened: list[list[str]] = []

    def __init__(self, *, config: dict[str, Any]) -> None:
        type(self).configs.append(config)

    @classmethod
    def listen_queues(cls, queues: list[str]) -> None:
        cls.listened.append(list(queues))

    @classmethod
    def launch(cls) -> None:
        return None


class DBOSExecutorIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeDBOS.configs = []
        _FakeDBOS.listened = []
        state_patch = patch.object(engine_dbos, "_STATE", engine_dbos._DBOSRuntimeState())
        state_patch.start()
        self.addCleanup(state_patch.stop)
        for name, value in (
            ("_load_dbos_api", lambda: (_FakeDBOS, None)),
            ("register_workflow", lambda dbos_class: None),
            ("ensure_queue_registered", AsyncMock()),
        ):
            p = patch.object(engine_dbos, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_defaults_to_host_executor_id(self) -> None:
        await engine_dbos.ensure_dbos_ready("postgresql://db/mash")

        self.assertEqual(_FakeDBOS.configs[0]["executor_id"], "host")
        self.assertEqual(engine_dbos.get_executor_id(), "host")

    async def test_configured_executor_id_is_passed_to_dbos(self) -> None:
        engine_dbos.configure_executor_id(engine_dbos.agent_executor_id("research"))

        await engine_dbos.ensure_dbos_ready("postgresql://db/mash")

        self.assertEqual(_FakeDBOS.configs[0]["executor_id"], "agent:research")

    async def test_changing_executor_id_after_launch_raises(self) -> None:
        await engine_dbos.ensure_dbos_ready("postgresql://db/mash")

        engine_dbos.configure_executor_id("host")
        with self.assertRaises(RuntimeError):
            engine_dbos.configure_executor_id("agent:research")

    async def test_host_listens_to_every_queue(self) -> None:
        await engine_dbos.ensure_dbos_ready("postgresql://db/mash")

        self.assertEqual(_FakeDBOS.listened, [])

    async def test_agent_process_uses_agent_executor_and_listens_to_no_queues(self) -> None:
        engine_dbos.configure_agent_process("research")

        await engine_dbos.ensure_dbos_ready("postgresql://db/mash")

        self.assertEqual(_FakeDBOS.configs[0]["executor_id"], "agent:research")
        self.assertEqual(_FakeDBOS.listened, [[]])

    def test_agent_executor_id_requires_agent_id(self) -> None:
        with self.assertRaises(ValueError):
            engine_dbos.agent_executor_id("  ")


if __name__ == "__main__":
    unittest.main()

"""Tests for BashTool lazy session startup."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from mash.tools.bash import (
    BashSession,
    BashTool,
    build_bash_env,
    resolve_bash_working_dir,
)


class _FakeStdin:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = []

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        return None


class BashToolTests(unittest.TestCase):
    def test_constructor_does_not_spawn_bash_process(self) -> None:
        with patch("mash.tools.bash.subprocess.Popen") as popen:
            tool = BashTool(working_dir="/tmp")
            try:
                self.assertFalse(popen.called)
            finally:
                tool.shutdown()

    def test_execute_starts_bash_process_on_first_use_and_reuses_it(self) -> None:
        with patch(
            "mash.tools.bash.subprocess.Popen",
            side_effect=lambda *args, **kwargs: _FakeProcess(),
        ) as popen:
            with patch.object(
                BashSession,
                "_read_until_sentinel",
                return_value=(["ok"], 0, 1),
            ):
                tool = BashTool(working_dir="/tmp")
                try:
                    first = asyncio.run(tool.execute({"command": "pwd"}))
                    second = asyncio.run(tool.execute({"command": "pwd"}))
                finally:
                    tool.shutdown()

        self.assertFalse(first.is_error)
        self.assertEqual(first.content, "ok")
        self.assertFalse(second.is_error)
        self.assertEqual(second.content, "ok")
        self.assertEqual(popen.call_count, 1)


class BashEnvTests(unittest.TestCase):
    def test_env_excludes_runtime_variables(self) -> None:
        with patch.dict(
            os.environ,
            {"MASH_DATABASE_URL": "postgresql://secret", "ANTHROPIC_API_KEY": "sk-secret"},
        ):
            env = build_bash_env(None, "/tmp")
            tool = BashTool(working_dir=tempfile.gettempdir())
            try:
                result = asyncio.run(tool.execute({"command": "env"}))
            finally:
                tool.shutdown()

        self.assertEqual(set(env), {"HOME", "PATH", "LANG", "PWD"})
        self.assertNotIn("secret", result.content)
        self.assertNotIn("MASH_DATABASE_URL", result.content)

    def test_explicit_working_dir_wins(self) -> None:
        self.assertEqual(resolve_bash_working_dir("/srv/repo"), "/srv/repo")

    def test_working_dir_defaults_to_agent_workspace_when_present(self) -> None:
        with patch("mash.tools.bash.os.path.isdir", return_value=True):
            self.assertEqual(resolve_bash_working_dir(None), "/home/agent/workspace")
        with patch("mash.tools.bash.os.path.isdir", return_value=False):
            self.assertIsNone(resolve_bash_working_dir(None))


if __name__ == "__main__":
    unittest.main()

"""Each agent in a sandbox gets a uid of its own and a folder only it can use.

Running code as those uids needs root, so these tests inject ``chown`` and the
passwd step and check what the daemon asks for; the real run is in the pilot
sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mash.sandbox.agent_users import FIRST_AGENT_UID, AgentUsers
from mash.sandbox.daemon import run_command


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class AgentUsersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.chowned: list[tuple[str, int]] = []
        self.added: list[tuple[int, str]] = []

    def _users(self) -> AgentUsers:
        return AgentUsers(
            str(self.root),
            isolate=True,
            chown=lambda path, uid: self.chowned.append((str(path), uid)),
            add_user=lambda uid, folder: self.added.append((uid, folder)),
        )

    def test_each_agent_gets_its_own_uid_and_keeps_it(self) -> None:
        users = self._users()
        users.prepare()
        research = users.get("research")
        writer = users.get("writer")

        self.assertEqual(research.uid, FIRST_AGENT_UID)
        self.assertEqual(writer.uid, FIRST_AGENT_UID + 1)
        self.assertEqual(users.get("research").uid, FIRST_AGENT_UID)
        # The map is on the volume: a new daemon over it gives the same uids.
        self.assertEqual(self._users().get("writer").uid, FIRST_AGENT_UID + 1)

    def test_the_folder_is_the_agents_alone(self) -> None:
        users = self._users()
        users.prepare()
        research = users.get("research")

        folder = self.root / "research"
        self.assertEqual(research.folder, str(folder))
        self.assertEqual(_mode(folder), 0o700)
        self.assertIn((str(folder), FIRST_AGENT_UID), self.chowned)
        self.assertIn((FIRST_AGENT_UID, str(folder)), self.added)
        # No agent may create or list folders at the top.
        self.assertEqual(_mode(self.root), 0o711)
        self.assertIn((str(self.root), 0), self.chowned)

    def test_the_map_is_readable_only_by_root(self) -> None:
        users = self._users()
        users.prepare()
        users.get("research")

        map_file = self.root / ".mash" / "uids.json"
        self.assertEqual(json.loads(map_file.read_text()), {"research": FIRST_AGENT_UID})
        self.assertEqual(_mode(map_file), 0o600)
        self.assertEqual(_mode(map_file.parent), 0o700)

    def test_an_old_folder_is_handed_over_without_following_links(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "secret"
        outside.write_text("x")
        folder = self.root / "research"
        (folder / "lib").mkdir(parents=True)
        (folder / "lib" / "sitecustomize.py").write_text("")
        (folder / "link").symlink_to(outside)

        users = self._users()
        users.prepare()
        users.get("research")

        chowned = {path for path, uid in self.chowned if uid == FIRST_AGENT_UID}
        self.assertEqual(
            chowned,
            {
                str(folder),
                str(folder / "lib"),
                str(folder / "lib" / "sitecustomize.py"),
                str(folder / "link"),
            },
        )
        self.assertNotIn(str(outside), {path for path, _ in self.chowned})

    def test_prepare_restores_the_passwd_entries_of_mapped_agents(self) -> None:
        users = self._users()
        users.prepare()
        users.get("research")
        users.get("writer")
        self.added.clear()

        self._users().prepare()

        self.assertEqual(
            sorted(self.added),
            [
                (FIRST_AGENT_UID, str(self.root / "research")),
                (FIRST_AGENT_UID + 1, str(self.root / "writer")),
            ],
        )

    def test_without_root_everything_runs_as_the_current_user(self) -> None:
        users = AgentUsers(str(self.root), isolate=False)
        users.prepare()
        research = users.get("research")

        self.assertIsNone(research.uid)
        self.assertTrue((self.root / "research").is_dir())
        self.assertFalse((self.root / ".mash").exists())


class AgentCommandTests(unittest.TestCase):
    def _launch(self, **kwargs) -> tuple[tuple, dict]:
        calls = []

        async def fake_exec(*args, **kw):
            calls.append((args, kw))
            raise RuntimeError("stop here")

        with patch.dict(os.environ, {"MASH_DATABASE_URL": "postgresql://secret"}), patch(
            "mash.sandbox.daemon.asyncio.create_subprocess_exec", fake_exec
        ):
            with self.assertRaises(RuntimeError):
                asyncio.run(run_command("ls", folder="/w/research", **kwargs))
        [call] = calls
        return call

    def test_an_agents_code_runs_as_its_uid_with_variables_in_the_environment(self) -> None:
        args, kwargs = self._launch(uid=20001, env={"MASH_SALES_DB_URL": "postgresql://ro"})

        self.assertEqual(
            args[:6],
            ("setpriv", "--reuid=20001", "--regid=20001", "--clear-groups", "--", "/bin/bash"),
        )
        self.assertEqual(args[-2:], ("/w/research", "ls"))
        # Other uids can read argv from /proc, so values never go there.
        self.assertFalse(any("postgresql://ro" in str(arg) for arg in args))
        self.assertEqual(kwargs["env"]["MASH_SALES_DB_URL"], "postgresql://ro")
        self.assertEqual(kwargs["env"]["HOME"], "/w/research")
        self.assertNotIn("MASH_DATABASE_URL", kwargs["env"])

    def test_without_a_uid_code_runs_as_the_current_user(self) -> None:
        args, kwargs = self._launch(env={"MASH_X": "v"})

        self.assertEqual(args[0], "/bin/bash")
        self.assertFalse(any(arg == "MASH_X=v" for arg in args))
        self.assertEqual(kwargs["env"]["MASH_X"], "v")


if __name__ == "__main__":
    unittest.main()

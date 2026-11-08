"""A uid of its own for each agent in a sandbox, and a folder only it can use.

The daemon runs as root and runs an agent's code, and its file reads and
writes, as that agent's uid. So one agent cannot read another's processes'
environment (``sandbox_env`` values), nor write into its folder, e.g. a
``sitecustomize.py`` the other agent's Python would load.

State lives on the user's volume, so it outlives the container:

- ``<workspaces>/.mash/uids.json`` maps each agent id to its uid, from
  ``FIRST_AGENT_UID`` up. Root only.
- ``<workspaces>/<agent_id>`` is owned by the agent's uid, mode 0700. The
  daemon creates it; the top folder is root's, mode 0711, so no agent can
  create, claim or list another's folder.

``/etc/passwd`` is the container's, so ``prepare`` adds an ``agent-<uid>``
entry for every mapped uid when the daemon starts: git and other tools fail
for a uid without one.

Without root (a developer's machine, the tests) nothing is isolated: every
agent runs as the current user in its folder.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

FIRST_AGENT_UID = 20000
_STATE_DIR = ".mash"
_MAP_FILE = "uids.json"
# The map's path under the workspaces folder.
UID_MAP = f"{_STATE_DIR}/{_MAP_FILE}"


@dataclass(frozen=True)
class AgentUser:
    """Where an agent's code runs, and as whom; ``uid`` None is the current user."""

    uid: Optional[int]
    folder: str


def add_passwd_entry(uid: int, folder: str) -> None:
    """Add user and group ``agent-<uid>`` with home ``folder``, unless the uid has one."""
    try:
        pwd.getpwuid(uid)
        return
    except KeyError:
        pass
    name = f"agent-{uid}"
    subprocess.run(["groupadd", "--gid", str(uid), name], check=True, capture_output=True)
    subprocess.run(
        [
            "useradd",
            "--uid", str(uid),
            "--gid", str(uid),
            "--no-create-home",
            "--home-dir", folder,
            "--shell", "/bin/bash",
            name,
        ],
        check=True,
        capture_output=True,
    )


class AgentUsers:
    """The uids and folders of one sandbox's agents."""

    def __init__(
        self,
        workspaces_root: str,
        *,
        isolate: Optional[bool] = None,
        chown: Callable[[str, int], None] = lambda path, uid: os.lchown(path, uid, uid),
        add_user: Callable[[int, str], None] = add_passwd_entry,
    ) -> None:
        self.workspaces_root = workspaces_root
        self.isolate = os.geteuid() == 0 if isolate is None else isolate
        self._chown = chown
        self._add_user = add_user
        self._state_dir = os.path.join(workspaces_root, _STATE_DIR)
        self._map_path = os.path.join(self._state_dir, _MAP_FILE)
        self._uids: dict[str, int] = self._load() if self.isolate else {}

    def prepare(self) -> None:
        """Lock down the top folder and restore the mapped agents' passwd entries."""
        os.makedirs(self.workspaces_root, exist_ok=True)
        if not self.isolate:
            return
        self._chown(self.workspaces_root, 0)
        os.chmod(self.workspaces_root, 0o711)
        os.makedirs(self._state_dir, exist_ok=True)
        self._chown(self._state_dir, 0)
        os.chmod(self._state_dir, 0o700)
        for agent_id, uid in sorted(self._uids.items()):
            self._add_user(uid, self._folder(agent_id))

    def get(self, agent_id: str) -> AgentUser:
        """The agent's uid and folder, creating both on first use.

        ``agent_id`` must already be a safe folder name.
        """
        folder = self._folder(agent_id)
        if not self.isolate:
            os.makedirs(folder, exist_ok=True)
            return AgentUser(uid=None, folder=folder)
        uid = self._uids.get(agent_id)
        if uid is None:
            uid = max(self._uids.values(), default=FIRST_AGENT_UID - 1) + 1
            self._uids[agent_id] = uid
            self._save()
            self._add_user(uid, folder)
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o700)
        if os.lstat(folder).st_uid != uid:
            self._hand_over(folder, uid)
        return AgentUser(uid=uid, folder=folder)

    def _folder(self, agent_id: str) -> str:
        return os.path.join(self.workspaces_root, agent_id)

    def _hand_over(self, folder: str, uid: int) -> None:
        # A folder from before agents had their own uids belongs to the old
        # shared one. Links are changed themselves, never followed: an agent
        # may have pointed one anywhere.
        self._chown(folder, uid)
        for parent, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                self._chown(os.path.join(parent, name), uid)

    def _load(self) -> dict[str, int]:
        try:
            with open(self._map_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        return {str(agent_id): int(uid) for agent_id, uid in data.items()}

    def _save(self) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        tmp = f"{self._map_path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._uids, handle, sort_keys=True)
        os.replace(tmp, self._map_path)


__all__ = ["FIRST_AGENT_UID", "UID_MAP", "AgentUser", "AgentUsers", "add_passwd_entry"]

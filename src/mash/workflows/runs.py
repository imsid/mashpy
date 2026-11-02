"""Workflow run ids and the runner id that scopes them.

A run id is ``mw:<runner_id>:<workflow_id>:<token>``. The runner id names the
deployment: the host recovers the unfinished runs under its own runner id, so
deployments that share one database give each pool its own.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid

_WORKFLOW_RUN_ID_PREFIX = "mw"
MASH_RUNNER_ID_ENV = "MASH_RUNNER_ID"
DEFAULT_RUNNER_ID = "default"
# A run id is colon-delimited, so the runner id may not contain a colon.
_RUNNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def resolve_runner_id(explicit_value: str | None = None) -> str:
    """This deployment's runner id: explicit, else ``MASH_RUNNER_ID``, else ``default``.

    It must be stable across restarts, since recovery finds a deployment's
    runs by it.
    """
    for candidate in (explicit_value, os.getenv(MASH_RUNNER_ID_ENV)):
        value = str(candidate or "").strip()
        if not value:
            continue
        if not _RUNNER_ID_PATTERN.match(value):
            raise ValueError(
                f"runner_id '{value}' is invalid: use letters, digits, '.', '_' "
                "or '-', starting with a letter or digit"
            )
        return value
    return DEFAULT_RUNNER_ID


def runner_run_id_prefix(runner_id: str) -> str:
    return f"{_WORKFLOW_RUN_ID_PREFIX}:{runner_id}:"


def workflow_run_id_prefix(runner_id: str, workflow_id: str) -> str:
    return f"{runner_run_id_prefix(runner_id)}{workflow_id}:"


def make_run_id(runner_id: str, workflow_id: str) -> str:
    token = secrets.token_urlsafe(12).rstrip("=")
    return f"{workflow_run_id_prefix(runner_id, workflow_id)}{token}"


def run_session_id(run_id: str) -> str:
    """The session every agent step of a run shares when the run names none."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mash.workflow.run:{run_id}"))


__all__ = [
    "DEFAULT_RUNNER_ID",
    "MASH_RUNNER_ID_ENV",
    "make_run_id",
    "resolve_runner_id",
    "run_session_id",
    "runner_run_id_prefix",
    "workflow_run_id_prefix",
]

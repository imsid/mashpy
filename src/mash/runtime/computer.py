"""Serving one agent of a pool on its own computer.

``mash agent serve`` imports the same ``build_pool()`` the host runs, serves one
agent from it behind an ``AgentServer``, and reaches the host for everything
else: subagent calls go to the host's H2A relay (``{host}/h2a``) with the
agent's token, and the host checks them against the request's host snapshot.
When the host has sandboxes, every agent served here gets a ``bash`` tool that
runs in the request user's sandbox through the host's ``/sandbox`` relay. When
the pool enables Cortex, it also gets read access to the request user's Cortex.
The pool itself is never started on a computer; it only supplies the agent's
spec and the metadata of the agents it may call.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from .client import AgentClient
from .engine.dbos import configure_agent_process
from .host.subagents import AgentMetadata
from .server import AgentServer

MASH_AGENT_TOKEN_ENV = "MASH_AGENT_TOKEN"
MASH_HOST_API_URL_ENV = "MASH_HOST_API_URL"
# Set to "1" by the host when it has sandboxes, so agents get bash.
MASH_SANDBOX_ENABLED_ENV = "MASH_SANDBOX_ENABLED"


class HostRelay:
    """Subagent access for a runtime on a computer: subagents resolve to the host."""

    def __init__(self, pool: Any, *, host_api_url: str, token: str) -> None:
        self._pool = pool
        self._relay_url = f"{host_api_url.rstrip('/')}/h2a"
        self._token = token
        self._clients: dict[str, AgentClient] = {}

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None:
        return self._pool.get_agent_metadata(agent_id)

    def get_client(self, agent_id: str) -> AgentClient:
        client = self._clients.get(agent_id)
        if client is None:
            client = AgentClient(self._relay_url, agent_id, token=self._token)
            self._clients[agent_id] = client
        return client


def build_agent_server(
    pool: Any,
    agent_id: str,
    *,
    token: str,
    host_api_url: str,
    runtime_database_url: str | None = None,
    sandbox_bash: bool = False,
) -> AgentServer:
    """Build the ``AgentServer`` for one agent of ``pool``.

    Configures this process's DBOS for the agent, so build at most one per
    process.
    """
    resolved_agent_id = str(agent_id or "").strip()
    spec = pool.get_registered_agent_spec(resolved_agent_id)
    if spec is None:
        raise ValueError(f"agent '{resolved_agent_id}' is not registered in the pool")
    if not str(token or "").strip():
        raise ValueError("agent token is required")
    if not str(host_api_url or "").strip():
        raise ValueError("host API URL is required")

    configure_agent_process(resolved_agent_id)
    server = AgentServer.from_spec(
        spec,
        runtime_database_url=runtime_database_url,
        session_id=str(uuid.uuid4()),
        token=token,
    )
    server.runtime.attach_subagent_access(
        HostRelay(pool, host_api_url=host_api_url, token=token)
    )
    if sandbox_bash:
        server.runtime.enable_sandbox_bash(host_api_url=host_api_url, token=token)
    if pool.cortex is not None:
        from mash.agents.cortex import CortexReader, CortexStore
        from mash.core.database import resolve_database_url

        database_url = runtime_database_url or resolve_database_url()
        server.runtime.enable_cortex(CortexReader(CortexStore(database_url), pool.cortex))
    return server


def resolve_agent_token(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_AGENT_TOKEN_ENV) or "").strip()
    return value or None


def resolve_host_api_url(explicit_value: str | None = None) -> str | None:
    value = str(explicit_value or os.environ.get(MASH_HOST_API_URL_ENV) or "").strip()
    return value or None


def resolve_sandbox_enabled() -> bool:
    return os.environ.get(MASH_SANDBOX_ENABLED_ENV, "").strip() == "1"


__all__ = [
    "MASH_AGENT_TOKEN_ENV",
    "MASH_HOST_API_URL_ENV",
    "MASH_SANDBOX_ENABLED_ENV",
    "HostRelay",
    "build_agent_server",
    "resolve_agent_token",
    "resolve_host_api_url",
    "resolve_sandbox_enabled",
]

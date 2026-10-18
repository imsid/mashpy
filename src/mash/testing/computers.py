"""Agent computers on localhost ports, for tests and Docker-free runs.

``LocalComputerProvider`` runs each agent's ``AgentServer`` in this process on
its own ``127.0.0.1`` port, and a host relay (``/h2a``) on another, so every
request, subagent call and workflow step takes the same H2A HTTP path as on
Docker computers. The servers share the pool's stores, standing in for the
shared Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI

from mash.api.routes.common import AppRuntimeState
from mash.api.routes.h2a import build_h2a_router
from mash.runtime.computer import HostRelay
from mash.runtime.host.computers import ComputerEndpoint
from mash.runtime.host.tokens import derive_agent_token
from mash.runtime.server import AgentServer
from mash.runtime.service import AgentRuntime

DEFAULT_LOCAL_SECRET = "local-computers"


class _Unowned:
    """A store shared with the pool: the agent server must not close it."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class _Server(uvicorn.Server):
    # Many servers share this process; none of them should take over signals.
    def capture_signals(self):  # type: ignore[override]
        return contextlib.nullcontext()


class LocalComputerProvider:
    """Each agent's ``AgentServer`` on a localhost port in this process."""

    def __init__(self, pool: Any, *, host_secret: str = DEFAULT_LOCAL_SECRET) -> None:
        self._pool = pool
        self._host_secret = host_secret
        self._servers: list[tuple[_Server, asyncio.Task[None]]] = []
        self._relay_url: str | None = None
        self._relay_lock = asyncio.Lock()
        self.agent_servers: dict[str, AgentServer] = {}

    async def ensure(self, agent_id: str) -> ComputerEndpoint:
        relay_url = await self._ensure_relay()
        token = derive_agent_token(self._host_secret, agent_id)
        spec = self._pool.get_registered_agent_spec(agent_id)
        shared_memory = self._pool.get_memory_store(agent_id)
        runtime = AgentRuntime.from_spec(
            spec,
            runtime_database_url=self._pool.runtime_database_url,
            session_id=str(uuid.uuid4()),
            runtime_store=_Unowned(self._pool.get_runtime_store()),
            memory_store=(
                _Unowned(shared_memory)
                if shared_memory is not None
                else spec.build_memory_store()
            ),
        )
        runtime.attach_subagent_access(
            HostRelay(self._pool, host_api_url=relay_url, token=token)
        )
        server = AgentServer(runtime, token=token)
        self.agent_servers[agent_id] = server
        port = await self._serve(server.app)
        return ComputerEndpoint(
            agent_id=agent_id, base_url=f"http://127.0.0.1:{port}", token=token
        )

    async def close(self) -> None:
        servers, self._servers = self._servers, []
        for server, _ in servers:
            server.should_exit = True
        await asyncio.gather(*(task for _, task in servers), return_exceptions=True)
        self.agent_servers.clear()
        self._relay_url = None

    async def _ensure_relay(self) -> str:
        async with self._relay_lock:
            if self._relay_url is None:
                relay = FastAPI()
                relay.include_router(build_h2a_router())
                relay.state.runtime_state = AppRuntimeState(
                    pool=self._pool,
                    api_event_store=None,
                    eval_service=None,
                    api_key=None,
                    default_events_limit=2000,
                    default_search_limit=10,
                    host_secret=self._host_secret,
                )
                port = await self._serve(relay)
                self._relay_url = f"http://127.0.0.1:{port}"
            return self._relay_url

    async def _serve(self, app: Any) -> int:
        server = _Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws="none")
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()  # raises the startup error
                raise RuntimeError("local computer server exited during startup")
            await asyncio.sleep(0.01)
        self._servers.append((server, task))
        return int(server.servers[0].sockets[0].getsockname()[1])


__all__ = ["LocalComputerProvider"]

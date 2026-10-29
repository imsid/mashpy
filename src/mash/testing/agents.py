"""Agents on localhost ports in this process, for tests.

``LocalAgentProvider`` runs each agent's ``AgentServer`` in this process on
its own ``127.0.0.1`` port, and a host relay (``/h2a``, ``/sandbox`` and
``/browser``) on another, so every request, subagent call, workflow step,
sandbox and browser call takes the
same HTTP path as with agent processes. The servers share the pool's stores,
standing in for the shared Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI

from mash.api.routes.common import AppRuntimeState
from mash.api.routes.browser import build_browser_link_router, build_browser_router
from mash.api.routes.h2a import build_h2a_router
from mash.api.routes.sandbox import build_link_router, build_sandbox_router
from mash.runtime.agent_process import HostRelay
from mash.runtime.host.processes import STATUS_RUNNING, AgentEndpoint
from mash.runtime.host.tokens import derive_agent_token
from mash.runtime.server import AgentServer
from mash.runtime.service import AgentRuntime

DEFAULT_LOCAL_SECRET = "local-agents"


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


class LocalAgentProvider:
    """Each agent's ``AgentServer`` on a localhost port in this process."""

    def __init__(self, pool: Any, *, host_secret: str = DEFAULT_LOCAL_SECRET) -> None:
        self._pool = pool
        self._host_secret = host_secret
        self._running: dict[str, tuple[_Server, asyncio.Task[None], AgentEndpoint]] = {}
        self._relay: tuple[_Server, asyncio.Task[None]] | None = None
        self._relay_url: str | None = None
        self._relay_lock = asyncio.Lock()
        self.agent_servers: dict[str, AgentServer] = {}

    def status(self, agent_id: str) -> str | None:
        return STATUS_RUNNING if agent_id in self._running else None

    async def ensure(self, agent_id: str) -> AgentEndpoint:
        running = self._running.get(agent_id)
        if running is not None:
            return running[2]
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
        if self._pool.get_sandboxes() is not None:
            runtime.enable_sandbox_tools(host_api_url=relay_url, token=token)
        browsers = getattr(self._pool, "get_browsers", lambda: None)()
        if browsers is not None and spec.browser_sites():
            runtime.enable_browser_tools(host_api_url=relay_url, token=token)
        server = AgentServer(runtime, token=token)
        self.agent_servers[agent_id] = server
        uvicorn_server, task, port = await self._serve(server.app)
        endpoint = AgentEndpoint(
            agent_id=agent_id, base_url=f"http://127.0.0.1:{port}", token=token
        )
        self._running[agent_id] = (uvicorn_server, task, endpoint)
        return endpoint

    async def stop(self, agent_id: str) -> None:
        running = self._running.pop(agent_id, None)
        self.agent_servers.pop(agent_id, None)
        if running is not None:
            await _shutdown([running[:2]])

    async def close(self) -> None:
        servers = [running[:2] for running in self._running.values()]
        if self._relay is not None:
            servers.append(self._relay)
        self._running.clear()
        self._relay = None
        await _shutdown(servers)
        self.agent_servers.clear()
        self._relay_url = None

    async def _ensure_relay(self) -> str:
        async with self._relay_lock:
            if self._relay_url is None:
                relay = FastAPI()
                relay.include_router(build_h2a_router())
                relay.include_router(build_sandbox_router())
                relay.include_router(build_link_router())
                relay.include_router(build_browser_router())
                relay.include_router(build_browser_link_router())
                relay.state.runtime_state = AppRuntimeState(
                    pool=self._pool,
                    api_event_store=None,
                    eval_service=None,
                    api_key=None,
                    default_events_limit=2000,
                    default_search_limit=10,
                    host_secret=self._host_secret,
                )
                server, task, port = await self._serve(relay)
                self._relay = (server, task)
                self._relay_url = f"http://127.0.0.1:{port}"
                # Links point at this relay, which also serves them.
                relay.state.runtime_state.public_url = self._relay_url
            return self._relay_url

    async def _serve(self, app: Any) -> tuple[_Server, asyncio.Task[None], int]:
        server = _Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws="none")
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()  # raises the startup error
                raise RuntimeError("local agent server exited during startup")
            await asyncio.sleep(0.01)
        return server, task, int(server.servers[0].sockets[0].getsockname()[1])


async def _shutdown(servers: list[tuple[_Server, asyncio.Task[None]]]) -> None:
    for server, _ in servers:
        server.should_exit = True
    await asyncio.gather(*(task for _, task in servers), return_exceptions=True)


__all__ = ["LocalAgentProvider"]

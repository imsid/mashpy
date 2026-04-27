"""Mash host for managing in-process per-agent runtime servers and clients."""

from __future__ import annotations

import asyncio
import importlib
import socket
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Dict, Optional

import uvicorn
from ..memory.compaction import compact_conversation
from .client import MashAgentClient
from .runtime import MashAgentRuntime, build_subagent_prompt_block
from .server import MashAgentServer
from .spec import AgentSpec
from .types import SubagentEndpoint, SubAgentMetadata


def _load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.MASHER_AGENT_ID,
        masher.build_masher_metadata,
        masher.create_masher_agent_spec,
    )


@dataclass
class HostedAgentHandle:
    """Local handle for manifest metadata and persisted state access."""

    agent_id: str
    definition: AgentSpec
    metadata: Optional[SubAgentMetadata]
    is_primary: bool
    subagent_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        resolved_agent_id = self.definition.get_agent_id().strip()
        if resolved_agent_id != self.agent_id:
            raise ValueError(
                "registered agent_id must match AgentSpec.get_agent_id() "
                f"(got {self.agent_id!r} vs {resolved_agent_id!r})"
            )
        config = self.definition.build_agent_config()
        if config.app_id != self.agent_id:
            raise ValueError(
                "registered agent_id must match build_agent_config().app_id "
                f"(got {self.agent_id!r} vs {config.app_id!r})"
            )
        self._config = config
        self._store = self.definition.build_memory_store()
        self._preview_tools = self.definition.build_tools()
        self._agent_preview = SimpleNamespace(tools=self._preview_tools)

    @property
    def store(self):
        return self._store

    @property
    def system_prompt(self):
        return self._config.system_prompt

    @property
    def agent(self):
        return self._agent_preview

    def get_model(self) -> str:
        return self.definition.build_llm().model

    def get_max_steps(self) -> int:
        return self._config.max_steps

    def get_subagent_ids(self) -> list[str]:
        return list(self.subagent_ids)

    def apply_subagent_metadata(self, subagents: dict[str, SubAgentMetadata]) -> None:
        if not self.is_primary or not subagents:
            return

        self._config.system_prompt = build_subagent_prompt_block(
            self._config.system_prompt, subagents
        )

    async def close(self) -> None:
        await self._preview_tools.shutdown()
        await self._store.close()

    async def get_session_total_tokens(self, session_id: str | None = None) -> int:
        target_session_id = str(session_id or "").strip()
        if not target_session_id:
            return 0
        turns = await self.store.get_turns(
            session_id=target_session_id,
            limit=1,
        )
        if not turns:
            return 0
        value = turns[-1].get("session_total_tokens", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def get_session_info(
        self, session_id: str | None = None
    ) -> dict[str, object]:
        return {
            "app_id": self.agent_id,
            "agent_id": self.agent_id,
            "session_id": session_id,
            "primary_agent_id": self.agent_id if self.is_primary else None,
            "subagent_ids": list(self.subagent_ids),
            "model": self.get_model(),
            "max_steps": self.get_max_steps(),
            "session_total_tokens": await self.get_session_total_tokens(session_id),
        }

    async def list_sessions(self) -> list[dict[str, object]]:
        if not hasattr(self.store, "list_sessions"):
            return []
        return await self.store.list_sessions(app_id=self.agent_id)

    async def get_history_turns(
        self, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, object]]:
        return await self.store.get_turns(
            session_id=session_id,
            limit=limit,
        )

    async def compact_session(
        self,
        session_id: str | None = None,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
    ) -> tuple[str | None, str | None]:
        target_session_id = (session_id or "").strip()
        if not target_session_id:
            raise ValueError("session_id is required")
        llm = self.definition.build_llm()
        return await compact_conversation(
            store=self.store,
            llm=llm,
            app_id=self.agent_id,
            session_id=target_session_id,
            max_tokens=self._config.max_tokens,
            temperature=self._config.compaction_temperature,
            turn_limit=self._config.compaction_turn_limit,
            reason=reason,
            session_total_tokens_reset=session_total_tokens_reset,
        )


@dataclass(frozen=True)
class _RegisteredAgent:
    agent_id: str
    definition: AgentSpec
    metadata: Optional[SubAgentMetadata]
    is_primary: bool
    bind_host: str | None = None
    bind_port: int | None = None


@dataclass(frozen=True)
class _RuntimeServerPlan:
    agent_id: str
    definition: AgentSpec
    bind_host: str
    bind_port: int
    is_primary: bool
    subagents: list[SubagentEndpoint] = field(default_factory=list)


@dataclass
class _AgentServer:
    runtime_server: MashAgentServer
    uvicorn_server: uvicorn.Server
    serve_task: asyncio.Task[None]
    base_url: str


class _EmbeddedUvicornServer(uvicorn.Server):
    """Uvicorn server variant for in-process embedded runtimes.

    Embedded runtime servers share one process with the outer host API, so they
    must not install or replay process-global signal handlers. The outer host
    server owns SIGINT/SIGTERM handling and shuts these embedded servers down by
    setting ``should_exit`` during host.close().
    """

    @contextmanager
    def capture_signals(self):
        yield


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connect_host(bind_host: str) -> str:
    return "127.0.0.1" if bind_host in {"", "0.0.0.0"} else bind_host


class MashAgentHost:
    """Host application managing in-process runtime servers and per-agent clients."""

    def __init__(self, *, bind_host: str = "127.0.0.1") -> None:
        self.bind_host = bind_host
        self._primary_agent_id: Optional[str] = None
        self._registered: Dict[str, _RegisteredAgent] = {}
        self._agents: Dict[str, HostedAgentHandle] = {}
        self._clients: Dict[str, MashAgentClient] = {}
        self._servers: Dict[str, _AgentServer] = {}

    def register_primary(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")
        if self._primary_agent_id is not None:
            raise ValueError("primary agent is already registered")

        self._registered[resolved_agent_id] = _RegisteredAgent(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
            bind_host=bind_host,
            bind_port=bind_port,
        )
        self._primary_agent_id = resolved_agent_id
        return resolved_agent_id

    def register_subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")

        self._registered[resolved_agent_id] = _RegisteredAgent(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=metadata,
            is_primary=False,
            bind_host=bind_host,
            bind_port=bind_port,
        )
        return resolved_agent_id

    def _resolve_local_agents(self) -> None:
        if self._agents:
            return
        for registered in self._registered.values():
            handle = HostedAgentHandle(
                agent_id=registered.agent_id,
                definition=registered.definition,
                metadata=registered.metadata,
                is_primary=registered.is_primary,
            )
            self._agents[registered.agent_id] = handle
        if self._primary_agent_id is not None:
            primary = self._agents[self._primary_agent_id]
            primary.subagent_ids = sorted(
                registered.agent_id
                for registered in self._registered.values()
                if not registered.is_primary and registered.metadata is not None
            )
            primary.apply_subagent_metadata(
                {
                    registered.agent_id: registered.metadata
                    for registered in self._registered.values()
                    if not registered.is_primary and registered.metadata is not None
                }
            )

    def _build_launch_plan(self) -> dict[str, _RuntimeServerPlan]:
        if self._primary_agent_id is None:
            raise ValueError("register_primary() must be called before start()")

        ports: dict[str, int] = {}
        for registered in self._registered.values():
            ports[registered.agent_id] = (
                int(registered.bind_port)
                if registered.bind_port is not None
                else _allocate_port()
            )

        subagent_endpoints = [
            SubagentEndpoint(
                agent_id=registered.agent_id,
                base_url=f"http://{_connect_host(registered.bind_host or self.bind_host)}:{ports[registered.agent_id]}",
                metadata=registered.metadata,
            )
            for registered in self._registered.values()
            if not registered.is_primary and registered.metadata is not None
        ]

        plan: dict[str, _RuntimeServerPlan] = {}
        for registered in self._registered.values():
            bind_host = registered.bind_host or self.bind_host
            plan[registered.agent_id] = _RuntimeServerPlan(
                agent_id=registered.agent_id,
                definition=registered.definition,
                bind_host=bind_host,
                bind_port=ports[registered.agent_id],
                is_primary=registered.is_primary,
                subagents=subagent_endpoints if registered.is_primary else [],
            )
        return plan

    async def _start_runtime_server(
        self,
        plan: _RuntimeServerPlan,
    ) -> _AgentServer:
        connect_host = _connect_host(plan.bind_host)
        base_url = f"http://{connect_host}:{plan.bind_port}"
        runtime = MashAgentRuntime.from_spec(plan.definition)
        if plan.is_primary and plan.subagents:
            runtime.configure_subagents(plan.subagents)
        runtime_server = MashAgentServer(runtime)
        config = uvicorn.Config(
            runtime_server.app,
            host=plan.bind_host,
            port=plan.bind_port,
            log_config=None,
            log_level=None,
        )
        server = _EmbeddedUvicornServer(config)
        serve_task = asyncio.create_task(
            server.serve(),
            name=f"MashAgentServer-{plan.agent_id}",
        )
        return _AgentServer(
            runtime_server=runtime_server,
            uvicorn_server=server,
            serve_task=serve_task,
            base_url=base_url,
        )

    async def _wait_until_ready(
        self,
        *,
        agent_id: str,
        server: _AgentServer,
        timeout_seconds: float = 20.0,
    ) -> MashAgentClient:
        started = time.time()
        await server.runtime_server.wait_until_ready(timeout=timeout_seconds)

        while time.time() - started < timeout_seconds:
            if server.serve_task.done():
                serve_error = None
                try:
                    serve_error = server.serve_task.exception()
                except asyncio.CancelledError:
                    serve_error = "server task cancelled"
                raise RuntimeError(
                    f"agent runtime '{agent_id}' exited before ready"
                    + (f": {serve_error}" if serve_error else "")
                )
            if bool(getattr(server.uvicorn_server, "started", False)):
                return MashAgentClient(server.base_url, agent_id)
            await asyncio.sleep(0.05)

        raise RuntimeError(
            f"agent runtime '{agent_id}' did not finish uvicorn startup before timeout"
        )

    async def start(self) -> None:
        if self._clients:
            return
        self._resolve_local_agents()
        launch_plan = self._build_launch_plan()
        started_agents: list[str] = []
        try:
            for agent_id in self._registered:
                agent_server = await self._start_runtime_server(launch_plan[agent_id])
                self._servers[agent_id] = agent_server
                started_agents.append(agent_id)
            for agent_id in started_agents:
                agent_server = self._servers[agent_id]
                self._clients[agent_id] = await self._wait_until_ready(
                    agent_id=agent_id,
                    server=agent_server,
                )
        except Exception:
            await self.close()
            raise

    def get_client(self, agent_id: str) -> MashAgentClient:
        client = self._clients.get(agent_id)
        if client is None:
            raise ValueError(f"agent client '{agent_id}' is not registered")
        return client

    def get_agent(self, agent_id: str) -> HostedAgentHandle:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"agent '{agent_id}' is not registered")
        return agent

    def list_agents(self) -> list[str]:
        return list(self._registered.keys())

    def get_primary_agent_id(self) -> str:
        if self._primary_agent_id is None:
            raise ValueError("primary agent is not registered")
        return self._primary_agent_id

    def describe_agents(self) -> list[dict[str, object]]:
        described: list[dict[str, object]] = []
        for registered in self._registered.values():
            described.append(
                {
                    "agent_id": registered.agent_id,
                    "role": "primary" if registered.is_primary else "subagent",
                    "metadata": (
                        asdict(registered.metadata)
                        if registered.metadata is not None
                        else None
                    ),
                }
            )
        return described

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

        for agent_server in self._servers.values():
            agent_server.uvicorn_server.should_exit = True
        for agent_server in self._servers.values():
            try:
                await asyncio.wait_for(agent_server.serve_task, timeout=5)
            except asyncio.TimeoutError:
                agent_server.uvicorn_server.force_exit = True
                await asyncio.wait_for(agent_server.serve_task, timeout=2)
        self._servers.clear()

        for agent in self._agents.values():
            await agent.close()

    async def __aenter__(self) -> "MashAgentHost":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.close()


class MashAgentHostBuilder:
    """Builder for composing one primary runtime and optional subagents."""

    def __init__(self) -> None:
        self._primary: _RegisteredAgent | None = None
        self._subagents: list[_RegisteredAgent] = []
        self._masher_enabled = False

    def primary(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> "MashAgentHostBuilder":
        if self._primary is not None:
            raise ValueError("primary agent is already configured")
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._primary = _RegisteredAgent(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
            bind_host=bind_host,
            bind_port=bind_port,
        )
        return self

    def enable_masher(self, enabled: bool = True) -> "MashAgentHostBuilder":
        self._masher_enabled = bool(enabled)
        return self

    def subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
        bind_host: str | None = None,
        bind_port: int | None = None,
    ) -> "MashAgentHostBuilder":
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._subagents.append(
            _RegisteredAgent(
                agent_id=resolved_agent_id,
                definition=definition,
                metadata=metadata,
                is_primary=False,
                bind_host=bind_host,
                bind_port=bind_port,
            )
        )
        return self

    def build(self, *, bind_host: str = "127.0.0.1") -> MashAgentHost:
        if self._primary is None:
            raise ValueError("primary agent is required")

        host = MashAgentHost(bind_host=bind_host)
        host.register_primary(
            self._primary.definition,
            agent_id=self._primary.agent_id,
            bind_host=self._primary.bind_host,
            bind_port=self._primary.bind_port,
        )
        for registered in self._subagents:
            metadata = registered.metadata
            if metadata is None:
                raise ValueError(f"subagent '{registered.agent_id}' is missing metadata")
            host.register_subagent(
                registered.definition,
                metadata=metadata,
                agent_id=registered.agent_id,
                bind_host=registered.bind_host,
                bind_port=registered.bind_port,
            )
        if self._masher_enabled:
            masher_agent_id, build_masher_metadata, create_masher_agent_spec = (
                _load_masher_components()
            )
            host.register_subagent(
                create_masher_agent_spec(target_app_id=self._primary.agent_id),
                agent_id=masher_agent_id,
                metadata=build_masher_metadata(),
            )
        return host


__all__ = ["HostedAgentHandle", "MashAgentHost", "MashAgentHostBuilder"]

"""Host for managing in-process per-agent runtime servers and clients."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Dict, Optional

from ..client import AgentClient, InProcessAgentClient
from ..factory import configure_subagent_tools
from ..service import AgentRuntime
from ..spec import AgentSpec
from .subagents import SubAgentMetadata, build_subagent_prompt_block
from .types import AgentRegistration


class AgentHost:
    """Host application managing in-process runtimes and per-agent clients."""

    def __init__(
        self,
        *,
        runtime_database_url: str | None = None,
    ) -> None:
        self.runtime_database_url = str(runtime_database_url or "").strip() or None
        self._primary_agent_id: Optional[str] = None
        self._registered: Dict[str, AgentRegistration] = {}
        self._agents: Dict[str, AgentRuntime] = {}
        self._clients: Dict[str, AgentClient] = {}

    def configure_runtime_database_url(self, database_url: str | None) -> None:
        value = str(database_url or "").strip()
        self.runtime_database_url = value or None

    def register_primary(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")
        if self._primary_agent_id is not None:
            raise ValueError("primary agent is already registered")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
        )
        self._primary_agent_id = resolved_agent_id
        return resolved_agent_id

    def register_subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=metadata,
            is_primary=False,
        )
        return resolved_agent_id

    async def start(self) -> None:
        if self._clients:
            return
        if not self.runtime_database_url:
            env_value = os.environ.get("MASH_RUNTIME_DATABASE_URL", "").strip()
            if env_value:
                self.runtime_database_url = env_value
        if not self.runtime_database_url:
            raise RuntimeError(
                "MASH_RUNTIME_DATABASE_URL is required to start hosted Mash runtimes"
            )
        try:
            for registered in self._registered.values():
                runtime = AgentRuntime.from_spec(
                    registered.definition,
                    runtime_database_url=self.runtime_database_url,
                    session_id=registered.session_id,
                )
                self._agents[registered.agent_id] = runtime

            if self._primary_agent_id is not None:
                primary = self._agents[self._primary_agent_id]
                subagent_metadata = {
                    registered.agent_id: registered.metadata
                    for registered in self._registered.values()
                    if not registered.is_primary and registered.metadata is not None
                }
                primary.set_subagent_ids(sorted(subagent_metadata.keys()))
                if subagent_metadata:
                    primary.set_subagent_clients(
                        {
                            agent_id: InProcessAgentClient(self._agents[agent_id])
                            for agent_id in sorted(subagent_metadata.keys())
                        }
                    )
                    primary.set_system_prompt(
                        build_subagent_prompt_block(
                            primary.system_prompt,
                            subagent_metadata,
                        )
                    )
                    configure_subagent_tools(
                        primary,
                        primary.agent,
                        session_id=primary.session_id,
                    )

            for agent_id, runtime in self._agents.items():
                await runtime.open()
                self._clients[agent_id] = InProcessAgentClient(runtime)
        except Exception:
            await self.close()
            raise

    def get_client(self, agent_id: str) -> AgentClient:
        client = self._clients.get(agent_id)
        if client is None:
            raise ValueError(f"agent client '{agent_id}' is not registered")
        return client

    def get_agent(self, agent_id: str) -> AgentRuntime:
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

        for agent in self._agents.values():
            await agent.shutdown()
        self._agents.clear()

    async def __aenter__(self) -> "AgentHost":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.close()


__all__ = ["AgentHost"]

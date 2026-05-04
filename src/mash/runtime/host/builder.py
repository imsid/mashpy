"""Builder for host composition."""

from __future__ import annotations

import importlib
import os

from .host import AgentHost
from .subagents import SubAgentMetadata
from .types import AgentRegistration
from ..spec import AgentSpec


def load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.MASHER_AGENT_ID,
        masher.build_masher_metadata,
        masher.create_masher_agent_spec,
    )


class HostBuilder:
    """Builder for composing one primary runtime and optional subagents."""

    def __init__(self) -> None:
        self._primary: AgentRegistration | None = None
        self._subagents: list[AgentRegistration] = []
        self._masher_enabled = False

    def primary(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        if self._primary is not None:
            raise ValueError("primary agent is already configured")
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._primary = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
        )
        return self

    def enable_masher(self, enabled: bool = True) -> "HostBuilder":
        self._masher_enabled = bool(enabled)
        return self

    def subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._subagents.append(
            AgentRegistration(
                agent_id=resolved_agent_id,
                definition=definition,
                metadata=metadata,
                is_primary=False,
            )
        )
        return self

    def build(self) -> AgentHost:
        if self._primary is None:
            raise ValueError("primary agent is required")

        host = AgentHost(runtime_database_url=os.environ.get("MASH_RUNTIME_DATABASE_URL"))
        host.register_primary(
            self._primary.definition,
            agent_id=self._primary.agent_id,
        )
        for registered in self._subagents:
            metadata = registered.metadata
            if metadata is None:
                raise ValueError(f"subagent '{registered.agent_id}' is missing metadata")
            host.register_subagent(
                registered.definition,
                metadata=metadata,
                agent_id=registered.agent_id,
            )
        if self._masher_enabled:
            masher_agent_id, build_masher_metadata, create_masher_agent_spec = (
                load_masher_components()
            )
            host.register_subagent(
                create_masher_agent_spec(target_app_id=self._primary.agent_id),
                agent_id=masher_agent_id,
                metadata=build_masher_metadata(),
            )
        return host

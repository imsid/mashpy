"""MashAgent host for managing local agent servers and clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Optional

from ..core.config import SystemPrompt
from ..tools.subagent import InvokeSubagentTool
from .client import MashAgentClient
from .server import MashAgentServer
from .spec import AgentSpec
from .types import SubAgentMetadata


@dataclass(frozen=True)
class _RegisteredAgent:
    agent_id: str
    definition: AgentSpec
    metadata: Optional[SubAgentMetadata]
    is_primary: bool


def build_subagent_prompt_block(
    base_prompt: SystemPrompt,
    subagents: Dict[str, SubAgentMetadata],
) -> SystemPrompt:
    """Append subagent routing guidance to a system prompt."""
    if not subagents:
        return base_prompt

    lines = [
        "SUBAGENTS",
        "Delegate work using InvokeSubagent(agent_id, prompt, opts).",
    ]
    for agent_id in sorted(subagents.keys()):
        meta = subagents[agent_id]
        capabilities = ", ".join(meta.capabilities)
        lines.append(f"- {agent_id} | {meta.display_name}: {meta.description}")
        lines.append(f"  Capabilities: {capabilities}")
        lines.append(f"  Guidance: {meta.usage_guidance}")
    lines.append(
        "When delegating, choose the best subagent id and pass a concise task prompt."
    )
    guidance = "\n".join(lines)

    if isinstance(base_prompt, list):
        return [*base_prompt, {"type": "text", "text": guidance}]
    return f"{base_prompt}\n\n{guidance}"


class MashAgentHost:
    """Host application managing MashAgent runtimes and 1:1 clients."""

    def __init__(self, *, bind_host: str = "127.0.0.1") -> None:
        self.bind_host = bind_host
        self._primary_agent_id: Optional[str] = None
        self._registered: Dict[str, _RegisteredAgent] = {}
        self._agents: Dict[str, MashAgentServer] = {}
        self._clients: Dict[str, MashAgentClient] = {}

    def register_primary(
        self, definition: AgentSpec, *, agent_id: Optional[str] = None
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
        )
        self._primary_agent_id = resolved_agent_id
        return resolved_agent_id

    def register_subagent(
        self,
        definition: AgentSpec,
        *,
        agent_id: Optional[str] = None,
        metadata: SubAgentMetadata,
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
        )
        return resolved_agent_id

    def start(self) -> None:
        if self._primary_agent_id is None:
            raise ValueError("register_primary() must be called before start()")
        if self._agents:
            return

        for agent_id, registered in self._registered.items():
            runtime = MashAgentServer.from_spec(registered.definition)
            base_url = runtime.start_http_server(
                agent_id=agent_id, host=self.bind_host, port=0
            )
            client = MashAgentClient(base_url, agent_id)
            self._agents[agent_id] = runtime
            self._clients[agent_id] = client

        self._configure_primary_subagents()

    def _configure_primary_subagents(self) -> None:
        if self._primary_agent_id is None:
            return

        primary = self._agents[self._primary_agent_id]
        subagents = {
            registered.agent_id: registered.metadata
            for registered in self._registered.values()
            if not registered.is_primary and registered.metadata is not None
        }
        primary.set_subagent_ids(sorted(subagents.keys()))
        if not subagents:
            return

        primary_prompt = build_subagent_prompt_block(primary.system_prompt, subagents)
        primary.set_system_prompt(primary_prompt)

        if "InvokeSubagent" in primary.agent.tools:
            primary.agent.tools.unregister("InvokeSubagent")

        primary.agent.tools.register(
            InvokeSubagentTool(
                client_resolver=self.get_client,
                primary_app_id=primary.agent.config.app_id,
                primary_session_id_provider=primary.get_current_processing_session_id,
                event_logger=primary.get_event_logger(),
            )
        )

    def get_client(self, agent_id: str) -> MashAgentClient:
        client = self._clients.get(agent_id)
        if client is None:
            raise ValueError(f"agent client '{agent_id}' is not registered")
        return client

    def get_agent(self, agent_id: str) -> MashAgentServer:
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

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

        for agent in self._agents.values():
            agent.shutdown()
        self._agents.clear()

    def __enter__(self) -> "MashAgentHost":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.close()


__all__ = ["MashAgentHost", "MashAgentHostBuilder", "build_subagent_prompt_block"]


class MashAgentHostBuilder:
    """Builder for composing one primary agent and optional subagents."""

    def __init__(self) -> None:
        self._primary: tuple[AgentSpec, str | None] | None = None
        self._subagents: list[tuple[AgentSpec, SubAgentMetadata, str | None]] = []
        self._masher_enabled = False

    def primary(
        self, spec: AgentSpec, *, agent_id: str | None = None
    ) -> "MashAgentHostBuilder":
        if self._primary is not None:
            raise ValueError("primary agent is already configured")
        self._primary = (spec, agent_id)
        return self

    def enable_masher(self, enabled: bool = True) -> "MashAgentHostBuilder":
        self._masher_enabled = bool(enabled)
        return self

    def subagent(
        self,
        spec: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
    ) -> "MashAgentHostBuilder":
        self._subagents.append((spec, metadata, agent_id))
        return self

    def build(self, *, bind_host: str = "127.0.0.1") -> MashAgentHost:
        if self._primary is None:
            raise ValueError("primary agent is required")

        host = MashAgentHost(bind_host=bind_host)
        primary_spec, primary_agent_id = self._primary
        host.register_primary(primary_spec, agent_id=primary_agent_id)
        for spec, metadata, agent_id in self._subagents:
            host.register_subagent(spec, metadata=metadata, agent_id=agent_id)
        if self._masher_enabled:
            from ..agents.masher import MasherAgentSpec, build_masher_metadata

            masher_log_file = primary_spec.get_log_destination()
            primary_app_id = primary_spec.build_agent_config().app_id
            host.register_subagent(
                MasherAgentSpec(
                    log_file=masher_log_file,
                    target_app_id=primary_app_id,
                ),
                metadata=build_masher_metadata(),
            )
        return host

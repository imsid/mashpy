"""Shared pool registration and host composition types."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Optional

from .directory import AgentMetadata
from ..spec import AgentSpec


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    definition: AgentSpec
    metadata: Optional[AgentMetadata]
    is_workflow_agent: bool = False
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class Host:
    """An immutable composition of pool agents and workflows.

    Every agent in a host is the same kind of member: the built-in
    ``companion`` routes each request to them and to the host's workflows.
    A session is never bound to a host; the client addresses a host
    explicitly per request, and the request carries a snapshot of this
    composition for durable replay.
    """

    host_id: str
    agents: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.host_id.strip():
            raise ValueError("host_id is required")
        seen: set[str] = set()
        for agent_id in self.agents:
            if not str(agent_id).strip():
                raise ValueError("host agent ids must be non-empty")
            if agent_id in seen:
                raise ValueError(f"host '{self.host_id}' has duplicate agent '{agent_id}'")
            seen.add(agent_id)

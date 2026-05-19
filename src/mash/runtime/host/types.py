"""Shared host registration types."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Optional

from .subagents import SubAgentMetadata
from ..spec import AgentSpec


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    definition: AgentSpec
    metadata: Optional[SubAgentMetadata]
    is_primary: bool
    is_workflow_agent: bool = False
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

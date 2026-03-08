"""Public types for mash-api app composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from mash.runtime import MashRuntimeDefinition, SubAgentMetadata


@dataclass(frozen=True)
class SubagentRegistration:
    """Subagent registration payload for host-backed API composition."""

    definition: MashRuntimeDefinition
    metadata: SubAgentMetadata
    agent_id: str | None = None


@dataclass(frozen=True)
class MashAPIAppSpec:
    """Application spec used by mash-api CLI loading."""

    definition: MashRuntimeDefinition
    subagents: Sequence[SubagentRegistration] = ()

"""Public runtime data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.context import Response


@dataclass(frozen=True)
class RuntimeTurnResult:
    """Structured result for one runtime turn."""

    session_id: str
    response: Response
    compaction_summary_text: Optional[str]
    compaction_summary_turn_id: Optional[str]
    session_total_tokens: int


@dataclass(frozen=True)
class SubAgentMetadata:
    """Host metadata used for subagent prompt injection and routing guidance."""

    display_name: str
    description: str
    capabilities: list[str]
    usage_guidance: str

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("subagent metadata display_name is required")
        if not self.description.strip():
            raise ValueError("subagent metadata description is required")
        if not self.usage_guidance.strip():
            raise ValueError("subagent metadata usage_guidance is required")
        if not self.capabilities:
            raise ValueError("subagent metadata capabilities must be non-empty")
        for capability in self.capabilities:
            if not str(capability).strip():
                raise ValueError("subagent metadata capabilities must be non-empty")


@dataclass(frozen=True)
class SubagentEndpoint:
    """Resolved host endpoint data for one subagent runtime."""

    agent_id: str
    base_url: str
    metadata: SubAgentMetadata

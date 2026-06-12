"""Agent metadata and subagent prompt helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...core.config import SystemPrompt


@dataclass(frozen=True)
class AgentMetadata:
    """Self-description an agent supplies at registration.

    Role-independent: the same metadata describes the agent whether a host
    composition uses it as a primary or a subagent.
    """

    display_name: str
    description: str
    capabilities: list[str]
    usage_guidance: str

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("agent metadata display_name is required")
        if not self.description.strip():
            raise ValueError("agent metadata description is required")
        if not self.usage_guidance.strip():
            raise ValueError("agent metadata usage_guidance is required")
        if not self.capabilities:
            raise ValueError("agent metadata capabilities must be non-empty")
        for capability in self.capabilities:
            if not str(capability).strip():
                raise ValueError("agent metadata capabilities must be non-empty")


class SubagentPoolAccess(Protocol):
    """Narrow pool surface a runtime needs to wire subagents per turn."""

    def get_client(self, agent_id: str) -> Any: ...

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None: ...


def build_subagent_prompt_block(
    base_prompt: SystemPrompt,
    subagents: dict[str, AgentMetadata],
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

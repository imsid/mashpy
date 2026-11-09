"""Agent metadata and the host agent directory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...core.config import SystemPrompt


@dataclass(frozen=True)
class AgentMetadata:
    """Self-description an agent supplies at registration.

    The companion routes by it: a host request lists each of the host's
    agents with its metadata in the companion's agent directory.
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


class HostAccess(Protocol):
    """What a runtime needs to reach a host's agents: their metadata and clients."""

    def get_client(self, agent_id: str) -> Any: ...

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None: ...


def _directory_entry(agent_id: str, meta: AgentMetadata | None) -> list[str]:
    if meta is None:
        return [f"- {agent_id}"]
    return [
        f"- {agent_id} | {meta.display_name}: {meta.description}",
        f"  Capabilities: {', '.join(meta.capabilities)}",
        f"  Guidance: {meta.usage_guidance}",
    ]


def build_agent_directory(
    base_prompt: SystemPrompt,
    agents: dict[str, AgentMetadata | None],
) -> SystemPrompt:
    """Append the ``AGENTS`` directory of a host's agents to a system prompt."""
    if not agents:
        return base_prompt

    lines = [
        "AGENTS",
        "Run them with RunSubAgents(requests=[{agent_id, prompt, session_id?}], timeout_seconds).",
        "Each item runs as its own request, and all items run at the same time.",
        "Split a task into several items only when its parts do not depend on each other.",
        "The host's agents:",
    ]
    for agent_id in sorted(agents.keys()):
        lines.extend(_directory_entry(agent_id, agents[agent_id]))
    guidance = "\n".join(lines)

    if isinstance(base_prompt, list):
        return [*base_prompt, {"type": "text", "text": guidance}]
    return f"{base_prompt}\n\n{guidance}"


def append_context_block(
    base_prompt: SystemPrompt,
    context: str | None,
) -> SystemPrompt:
    """Append caller-supplied per-request context to a system prompt.

    ``context`` is injected verbatim (the caller owns its formatting). An
    empty or whitespace-only value leaves the prompt unchanged.
    """
    text = str(context or "").strip()
    if not text:
        return base_prompt

    if isinstance(base_prompt, list):
        return [*base_prompt, {"type": "text", "text": text}]
    return f"{base_prompt}\n\n{text}"

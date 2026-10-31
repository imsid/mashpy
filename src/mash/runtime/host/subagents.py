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


class SubagentAccess(Protocol):
    """What a runtime needs to wire subagents per turn: their metadata and clients."""

    def get_client(self, agent_id: str) -> Any: ...

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None: ...


def _directory_entry(agent_id: str, meta: AgentMetadata | None, *, you: bool) -> list[str]:
    label = f"{agent_id} (you)" if you else agent_id
    if meta is None:
        return [f"- {label}"]
    return [
        f"- {label} | {meta.display_name}: {meta.description}",
        f"  Capabilities: {', '.join(meta.capabilities)}",
        f"  Guidance: {meta.usage_guidance}",
    ]


def build_subagent_prompt_block(
    base_prompt: SystemPrompt,
    subagents: dict[str, AgentMetadata],
    *,
    own_agent_id: str | None = None,
    own_metadata: AgentMetadata | None = None,
) -> SystemPrompt:
    """Append the RunSubAgents directory to a system prompt.

    The directory lists the agent itself first, marked ``(you)``, then the
    host's subagents, so the model chooses among all the agents an item can
    run on from one list.
    """
    if not subagents:
        return base_prompt

    lines = [
        "SUBAGENTS",
        "Delegate work using RunSubAgents(requests=[{agent_id, prompt}], timeout_seconds).",
        "Each item runs as its own request, and all items run at the same time.",
        "Split a task into several items when it has parts that do not depend on "
        "each other, so they run at once. Answer directly when the task is small, "
        "or when each step needs the result of the one before.",
        "An item can run on any agent below, including you:",
    ]
    if own_agent_id:
        lines.extend(_directory_entry(own_agent_id, own_metadata, you=True))
    for agent_id in sorted(subagents.keys()):
        lines.extend(_directory_entry(agent_id, subagents[agent_id], you=False))
    lines.append(
        "Choose the best agent for each item and pass a concise task prompt."
    )
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

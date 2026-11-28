"""Agent metadata, host access, and per-request context in a system prompt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...core.config import SystemPrompt


@dataclass(frozen=True)
class AgentMetadata:
    """Self-description an agent supplies at registration.

    The companion routes by it: its classifier reads each of the host's
    agents' metadata, and ``about`` answers from it.
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

    def get_workflow(self, workflow_id: str) -> Any: ...

    def get_workflow_client(self) -> Any: ...


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

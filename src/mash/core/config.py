"""Configuration for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AgentConfig:
    """Configuration for agent behavior."""

    app_id: str
    system_prompt: str
    model: str = "claude-sonnet-4"
    max_steps: int = 20
    max_tokens: int = 4096
    temperature: float = 1.0
    api_key: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration."""
        if not self.app_id:
            raise ValueError("app_id is required")
        if not self.system_prompt:
            raise ValueError("system_prompt is required")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

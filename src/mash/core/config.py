"""Configuration for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

SystemPrompt = str | List[Dict[str, Any]]


@dataclass
class AgentConfig:
    """Configuration for agent behavior."""

    app_id: str
    system_prompt: SystemPrompt
    max_steps: int = 30
    max_tokens: int = 4096
    temperature: float = 1.0
    skills_enabled: bool = False
    prompt_caching_enabled: bool = True
    streaming_enabled: bool = True
    conversation_history_turns: int = 3
    compaction_token_threshold: int = 0
    compaction_turn_limit: int = 50
    compaction_temperature: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration."""
        if not self.app_id:
            raise ValueError("app_id is required")
        if isinstance(self.system_prompt, str):
            if not self.system_prompt:
                raise ValueError("system_prompt is required")
        elif not self.system_prompt:
            raise ValueError("system_prompt is required")
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.conversation_history_turns < 0:
            raise ValueError("conversation_history_turns must be >= 0")
        if self.compaction_token_threshold < 0:
            raise ValueError("compaction_token_threshold must be >= 0")
        if self.compaction_turn_limit <= 0:
            raise ValueError("compaction_turn_limit must be > 0")

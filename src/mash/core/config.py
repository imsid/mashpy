"""Configuration for agent execution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Tool search feature constants
TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_bm25_20251119"
TOOL_SEARCH_TOOL_NAME = "tool_search_tool_bm25"
TOOL_SEARCH_BETAS: List[str] = ["advanced-tool-use-2025-11-20"]

load_dotenv()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

SystemPrompt = str | List[Dict[str, Any]]


@dataclass
class AgentConfig:
    """Configuration for agent behavior."""

    app_id: str
    system_prompt: SystemPrompt
    model: str = ANTHROPIC_MODEL
    max_steps: int = 30
    max_tokens: int = 4096
    temperature: float = 1.0
    api_key: Optional[str] = None
    tool_search_enabled: bool = False
    prompt_caching_enabled: bool = True
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

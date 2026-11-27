"""Configuration for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

SystemPrompt = str | List[Dict[str, Any]]

@dataclass
class AgentConfig:
    """Configuration for agent behavior."""

    agent_id: str
    system_prompt: SystemPrompt
    max_steps: int = 30
    max_tokens: int = 4096
    temperature: float = 1.0
    skills_enabled: bool = True
    prompt_caching_enabled: bool = True
    streaming_enabled: bool = True
    # A request replays the text of the session's last this-many turns: each
    # user message and reply, with the agents and workflows that handled it.
    # Tool calls and results are not replayed. 0 is no history.
    conversation_history_turns: int = 3
    max_parallel_tools: int = 8
    # How long a RunSubAgents call waits for its children, by default and at most.
    subagent_timeout_seconds: int = 3600
    # The session compacts before a request once the latest turn's context
    # (all input of its last model call, cached included, plus output) reaches this. 0
    # turns compaction off.
    compaction_token_threshold: int = 500_000
    # A tool result longer than this is saved whole to a file in the agent's
    # sandbox folder, and the model gets a note with its size and path to read
    # it with run_code; without a sandbox the model gets the whole result. The
    # event log keeps the full result either way.
    tool_result_context_chars: int = 20_000
    compaction_temperature: float = 0.0
    # Sent with every model call of the agent, e.g. {"thinking_level": "low"}
    # for Gemini. Options are provider-specific: OpenAI, Anthropic and the
    # open-model providers pass keys they don't handle to their API.
    provider_options: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration."""
        if not self.agent_id:
            raise ValueError("agent_id is required")
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
        if self.max_parallel_tools < 1:
            raise ValueError("max_parallel_tools must be >= 1")
        if isinstance(self.subagent_timeout_seconds, bool) or self.subagent_timeout_seconds <= 0:
            raise ValueError("subagent_timeout_seconds must be positive")
        if self.compaction_token_threshold < 0:
            raise ValueError("compaction_token_threshold must be >= 0")
        if self.tool_result_context_chars < 1000:
            raise ValueError("tool_result_context_chars must be at least 1000")

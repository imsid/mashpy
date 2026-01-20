"""Dataclasses for mashd agent workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .telemetry import TokenUsage


@dataclass
class AgentConfig:
    """Configuration for agent workflows."""

    app_id: str
    system_prompt: str = ""
    model: str = ""
    max_steps: int = 4
    max_tokens: int = 1024
    max_history_messages: int = 10
    tool_search_enabled: bool = True
    anthropic_api_key: Optional[str] = None
    use_bash_tool: bool = False
    bash_working_dir: Optional[str] = None


@dataclass
class ToolCall:
    """Represents a requested tool call."""

    tool_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AgentStep:
    """One step in the agent loop."""

    step_id: int
    tool_calls: List[ToolCall]
    usage: TokenUsage


@dataclass
class AgentReply:
    """Reply payload returned to the caller."""

    text: str
    steps: List[AgentStep]
    usage: TokenUsage
    trace_id: str


@dataclass
class Context:
    """Phase 1 context bundle for the agent loop."""

    session_id: str
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    system_prompt: str
    metadata: Dict[str, Any]


@dataclass
class Action:
    """Phase 2 action results from the agent loop."""

    assistant_text: str
    tool_calls: List[ToolCall]
    tool_results: List[Dict[str, Any]]
    tokens_used: Dict[str, int]
    is_complete: bool
    assistant_blocks: List[Dict[str, Any]]


@dataclass
class Decision:
    """Phase 3 decision about whether to continue the agent loop."""

    should_continue: bool
    reason: str
    final_reply: Optional[AgentReply]

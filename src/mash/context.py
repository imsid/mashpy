"""Shared session state definitions for MCP CLI applications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from mashnet.host import Host

from .memory import Memory
from .render import Renderer

if TYPE_CHECKING:
    from .agent import AgentRuntime


@dataclass
class CLIContext:
    """Context object passed to command handlers."""

    app_name: str
    host: Host
    memory: Memory
    renderer: Renderer
    session_id: str
    agent_trace_id: Optional[str] = None
    agent: Optional["AgentRuntime"] = None

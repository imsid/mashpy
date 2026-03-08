"""App definition contract for composing Mash agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, List

from mash.mcp.types import MCPServerConfig

from ..core.config import AgentConfig
from ..core.llm import LLMProvider
from ..memory.store import MemoryStore
from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .server import MashAgentServer


class MashRuntimeDefinition(ABC):
    """Definition object describing how to build a Mash runtime."""

    @abstractmethod
    def get_app_id(self) -> str:
        """Return the stable app ID used for storage/logging scope."""

    @abstractmethod
    def build_store(self) -> MemoryStore:
        """Construct the app memory store."""

    @abstractmethod
    def build_tools(self) -> ToolRegistry:
        """Construct the app tool registry."""

    @abstractmethod
    def build_skills(self) -> SkillRegistry:
        """Construct the app skill registry."""

    @abstractmethod
    def build_llm(self) -> LLMProvider:
        """Construct the app LLM provider."""

    @abstractmethod
    def build_agent_config(self) -> AgentConfig:
        """Construct the app agent configuration."""

    @abstractmethod
    def get_log_destination(self) -> Path:
        """Return the path for structured event logs."""

    def build_mcp_servers(self) -> List[MCPServerConfig]:
        """Build typed MCP server configs for the app."""
        return []

    def enable_runtime_tools(self) -> bool:
        """Whether Mash runtime tools should be auto-registered."""
        return True

    def on_startup(self, runtime: "MashAgentServer") -> None:
        """Hook called after runtime initialization."""
        del runtime

    def on_shutdown(self, runtime: "MashAgentServer") -> None:
        """Hook called before runtime cleanup."""
        del runtime

"""Agent specification contract for composing Mash agents."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, List

from mash.mcp.types import MCPServerConfig

from ..core.config import AgentConfig
from ..core.llm import LLMProvider
from ..memory.store import MemoryStore, SQLiteStore
from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .server import MashAgentServer


class AgentSpec(ABC):
    """Single-agent build contract used by the Mash SDK."""

    DEFAULT_DATA_ROOT = Path("/var/lib/mash")

    @abstractmethod
    def get_agent_id(self) -> str:
        """Return the stable agent id used for storage, routing, and logs."""

    def build_store(self) -> MemoryStore:
        """Construct the agent memory store.

        By default, Mash provisions a SQLite store at:
        `<data_root>/<agent_id>/state.db`
        """
        return SQLiteStore(self.get_agent_data_dir() / "state.db")

    @abstractmethod
    def build_tools(self) -> ToolRegistry:
        """Construct the agent tool registry."""

    @abstractmethod
    def build_skills(self) -> SkillRegistry:
        """Construct the agent skill registry."""

    @abstractmethod
    def build_llm(self) -> LLMProvider:
        """Construct the agent LLM provider."""

    @abstractmethod
    def build_agent_config(self) -> AgentConfig:
        """Construct the agent runtime configuration."""

    def get_log_destination(self) -> Path:
        """Return the path for structured event logs.

        By default, Mash writes JSONL event logs at:
        `<data_root>/<agent_id>/logs/events.jsonl`
        """
        return self.get_agent_data_dir() / "logs" / "events.jsonl"

    def get_agent_data_dir(self) -> Path:
        """Return the agent-specific persistent data directory."""
        raw_value = os.getenv("MASH_DATA_DIR", "").strip()
        data_root = Path(raw_value).expanduser() if raw_value else self.DEFAULT_DATA_ROOT
        return data_root / self.get_agent_id()

    def build_mcp_servers(self) -> List[MCPServerConfig]:
        """Build typed MCP server configs for the agent."""
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


__all__ = ["AgentSpec"]

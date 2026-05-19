"""Agent specification contract for composing Mash agents."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, List

from mash.mcp.types import MCPServerConfig

from ..core.config import AgentConfig
from ..core.llm import LLMProvider
from ..memory.store import MemoryStore, PostgresStore, SQLiteStore
from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry

if TYPE_CHECKING:
    from .service import AgentRuntime


class AgentSpec(ABC):
    """Single-agent build contract used by the Mash SDK."""

    DEFAULT_DATA_ROOT = Path("/var/lib/mash")

    @classmethod
    def get_data_root(cls) -> Path:
        """Return the resolved persistent data root for this process."""
        raw_value = os.getenv("MASH_DATA_DIR", "").strip()
        if not raw_value:
            return cls.DEFAULT_DATA_ROOT

        data_root = Path(raw_value).expanduser()
        if data_root.is_absolute():
            return data_root.resolve()
        return (Path.cwd() / data_root).resolve()

    @abstractmethod
    def get_agent_id(self) -> str:
        """Return the stable agent id used for storage, routing, and logs."""

    def build_memory_store(self) -> MemoryStore:
        """Construct the agent memory store.

        By default, Mash provisions:
        - a Postgres store when `MASH_MEMORY_DATABASE_URL` is set
        - otherwise a SQLite store at `<data_root>/<agent_id>/state.db`
        """
        memory_database_url = os.getenv("MASH_MEMORY_DATABASE_URL", "").strip()
        if memory_database_url:
            return PostgresStore(memory_database_url)
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

    def get_log_destination(self) -> MemoryStore:
        """Return the MemoryStore used for structured event persistence."""
        return self.build_memory_store()

    def get_agent_data_dir(self) -> Path:
        """Return the agent-specific persistent data directory."""
        return self.get_data_root() / self.get_agent_id()

    def build_mcp_servers(self) -> List[MCPServerConfig]:
        """Build typed MCP server configs for the agent."""
        return []

    def enable_runtime_tools(self) -> bool:
        """Whether Mash runtime tools should be auto-registered."""
        return True

    def on_startup(self, runtime: "AgentRuntime") -> None:
        """Hook called after runtime initialization."""
        del runtime

    def on_shutdown(self, runtime: "AgentRuntime") -> None:
        """Hook called before runtime cleanup."""
        del runtime


__all__ = ["AgentSpec"]

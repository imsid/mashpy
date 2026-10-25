"""Agent specification contract for composing Mash agents."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, List

from mash.core.database import resolve_database_url
from mash.mcp.types import MCPServerConfig

from ..core.config import AgentConfig
from ..core.llm import LLMProvider
from ..memory.store import MemoryStore, PostgresStore
from ..skills.registry import SkillRegistry
from ..tools.registry import ToolRegistry
from ..tools.web_search import WebSearchProvider

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

        Requires ``MASH_DATABASE_URL`` to be set. Override this method to
        supply a custom store implementation.
        """
        memory_database_url = resolve_database_url()
        if not memory_database_url:
            raise RuntimeError(
                "MASH_DATABASE_URL is required. "
                "Set it to a Postgres connection string before starting the agent."
            )
        return PostgresStore(memory_database_url)

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

    def enable_sandbox(self) -> bool:
        """Whether the agent gets ``run_code``, ``sandbox_put`` and ``sandbox_get``.

        True by default: on a host with sandboxes, every agent can run code in
        the request user's sandbox. Return False for an agent whose model should
        never reach it, such as one that reads untrusted content and holds
        credentials.
        """
        return True

    def sandbox_file_roots(self) -> List[Path]:
        """Folders on this agent's machine that ``sandbox_put`` and ``sandbox_get`` may use.

        Empty by default, and then the agent gets neither tool: only
        ``run_code``. Return absolute paths to give the agent both, confined to
        regular files inside these folders. ``/proc``, ``/sys`` and ``/dev`` are
        refused even inside a root. Have the agent's tools write the data meant
        for the sandbox into a root, and keep credentials and other secrets out
        of every root: code in the sandbox can reach the internet.
        """
        return []

    def build_web_search(self) -> WebSearchProvider | None:
        """Web search provider backing `web_search`/`web_fetch`.

        Returns None by default — web search stays off until a spec returns a
        provider here. There is no default provider: naming one is an explicit
        choice about which third party receives your search data. For Parallel
        AI, return `ParallelSearchProvider(api_key=...)`.
        """
        return None

    def on_startup(self, runtime: "AgentRuntime") -> None:
        """Hook called after runtime initialization."""
        del runtime

    def on_shutdown(self, runtime: "AgentRuntime") -> None:
        """Hook called before runtime cleanup."""
        del runtime


__all__ = ["AgentSpec"]

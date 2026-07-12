"""Agent runtime service without transport concerns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Sequence

from mash.core.database import resolve_database_url
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig

from ..core.config import SystemPrompt
from ..logging import EventLogger
from ..memory.signals import build_default_signal_collector
from ..memory.store import MemoryStore
from . import context as context_helpers
from . import factory as factory_helpers
from . import requests as request_helpers
from .engine import DBOSRequestEngine, RequestEngine
from .events import RuntimeStore
from .spec import AgentSpec

if TYPE_CHECKING:
    from .host.subagents import SubagentPoolAccess


def _resolve_runtime_database_url(explicit_value: str | None = None) -> str:
    value = resolve_database_url(explicit_value)
    if not value:
        raise RuntimeError("MASH_DATABASE_URL is required for hosted runtime execution")
    return value


class AgentRuntime:
    """Async-native runtime core that owns request lifecycle and execution state."""

    def __init__(
        self,
        definition: AgentSpec,
        *,
        runtime_database_url: str | None = None,
        session_id: str,
        runtime_store: RuntimeStore,
        memory_store: MemoryStore,
    ) -> None:
        self.definition = definition
        self.app_id = definition.get_agent_id()
        self.runtime_database_url = _resolve_runtime_database_url(runtime_database_url)
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a string")
        self.session_id = session_id.strip()
        if not self.session_id:
            raise ValueError("session_id is required")

        self.memory_store = memory_store
        self.runtime_store = runtime_store
        self.engine: RequestEngine = DBOSRequestEngine(
            self,
            database_url=self.runtime_database_url,
        )
        self.store = self.memory_store
        self.event_logger = EventLogger(self.runtime_store)
        self.signal_collector = build_default_signal_collector()

        self._is_open = False
        self._shutdown_started = False
        self._shutdown_complete = False

        self._pool: SubagentPoolAccess | None = None
        self._mcp_servers: Sequence[MCPServerConfig] = definition.build_mcp_servers()
        self.has_mcp_manager = False
        self.mcp_manager: Optional[MCPManager] = None
        self._chain_renderer: Any = None

        self.agent = factory_helpers.build_agent_instance(
            self,
            session_id=self.session_id,
        )
        self._shared_llm = self.agent.llm
        self.agent.set_event_logger(self.event_logger, self.session_id)
        self.agent.llm.set_event_logger(
            self.event_logger,
            self.session_id,
            self.app_id,
        )
        self.system_prompt: SystemPrompt = self.agent.config.system_prompt
        self.tools = self.agent.tools
        self.skills = self.agent.skills

        definition.on_startup(self)

    @classmethod
    def from_spec(
        cls,
        definition: AgentSpec,
        *,
        runtime_database_url: str | None = None,
        session_id: str,
        runtime_store: RuntimeStore,
        memory_store: MemoryStore,
    ) -> "AgentRuntime":
        return cls(
            definition,
            runtime_database_url=runtime_database_url,
            session_id=session_id,
            runtime_store=runtime_store,
            memory_store=memory_store,
        )

    def get_event_logger(self) -> Any:
        return self.event_logger

    def set_chain_renderer(self, renderer: Any) -> None:
        self._chain_renderer = renderer
        self.agent.set_chain_renderer(renderer)

    def get_chain_renderer(self) -> Any:
        return self._chain_renderer

    def get_model(self) -> str:
        return self.agent.llm.model

    def get_max_steps(self) -> int:
        return self.agent.config.max_steps

    def get_mcp_servers(self) -> Sequence[MCPServerConfig]:
        return self._mcp_servers

    def attach_pool(self, pool: SubagentPoolAccess) -> None:
        self._pool = pool

    def get_pool(self) -> SubagentPoolAccess | None:
        return self._pool

    async def get_session_info(
        self,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await context_helpers.get_session_info(self, session_id)

    async def get_history_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await context_helpers.get_history_turns(self, session_id, limit=limit)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await context_helpers.list_sessions(self)

    async def get_session_signals(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await context_helpers.get_session_signals(self, session_id, limit=limit)

    def get_signal_definitions(self) -> dict[str, dict[str, Any]]:
        return dict(self.signal_collector.get_signal_definitions())

    async def get_session_total_tokens(
        self,
        session_id: str | None = None,
    ) -> int:
        return await context_helpers.get_session_total_tokens(self, session_id)

    async def compact_session(
        self,
        session_id: str | None = None,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
    ) -> tuple[str | None, str | None]:
        return await context_helpers.compact_session(
            self,
            session_id,
            reason=reason,
            session_total_tokens_reset=session_total_tokens_reset,
        )

    async def submit_request(
        self,
        *,
        message: str,
        session_id: str,
        structured_output: Any = None,
        host_snapshot: dict[str, Any] | None = None,
        context: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await request_helpers.submit_request(
            self,
            message=message,
            session_id=session_id,
            structured_output=structured_output,
            host_snapshot=host_snapshot,
            context=context,
            metadata=metadata,
        )

    async def submit_subagent_request(
        self,
        *,
        message: str,
        session_id: str,
        primary_session_id: str,
        primary_app_id: str,
        subagent_id: str,
        subagent_invoke_opts: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await request_helpers.submit_subagent_request(
            self,
            message=message,
            session_id=session_id,
            primary_session_id=primary_session_id,
            primary_app_id=primary_app_id,
            subagent_id=subagent_id,
            subagent_invoke_opts=subagent_invoke_opts,
            metadata=metadata,
        )

    async def stream_response_events(
        self,
        request_id: str,
        *,
        cursor: int = 0,
        wait_timeout: float = 15.0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        return await request_helpers.stream_response_events(
            self,
            request_id,
            cursor=cursor,
            wait_timeout=wait_timeout,
        )

    async def get_request_status(self, request_id: str) -> dict[str, Any]:
        self.require_open()
        return await self.engine.get_request_status(request_id=request_id)

    async def resume_request(self, request_id: str) -> dict[str, Any]:
        self.require_open()
        return await self.engine.resume_request(request_id=request_id)

    def require_open(self) -> None:
        if not self._is_open:
            raise RuntimeError(
                "AgentRuntime must be opened before request submission or streaming"
            )

    def configure_turn_context(
        self, agent: Any, *, session_id: str, trace_id: str
    ) -> None:
        agent.set_event_logger(self.event_logger, session_id)
        agent.llm.set_event_logger(self.event_logger, session_id, self.app_id)
        agent.set_trace_id(trace_id)

    def build_turn_agent(
        self,
        *,
        session_id: str,
        trace_id: str,
        host: dict[str, Any] | None = None,
    ) -> Any:
        agent = factory_helpers.build_agent_instance(
            self, session_id=session_id, shared_llm=self._shared_llm, host=host,
        )
        self.configure_turn_context(agent, session_id=session_id, trace_id=trace_id)
        return agent

    async def open(self) -> None:
        if self._is_open:
            return
        await self.store.open()
        await self.runtime_store.open()
        await self.engine.open()
        self._is_open = True

    async def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_started = True
        try:
            self._pool = None

            if self.has_mcp_manager and self.mcp_manager is not None:
                self.mcp_manager.disconnect_all()
            await self.agent.tools.shutdown()
            await self.agent.llm.close()
            await self.engine.close()
            self._is_open = False
        finally:
            self.definition.on_shutdown(self)
            self._shutdown_complete = True


__all__ = ["AgentRuntime"]

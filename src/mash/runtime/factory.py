"""Agent runtime factory and configuration helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Sequence

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig
from mash.tools.mcp import MCPToolAdapter
from mash.skills.tool import SkillTool

from ..core.agent import Agent
from ..core.config import SystemPrompt
from ..memory.signals import build_default_signal_collector
from ..tools.runtime import RuntimeToolBuilder
from ..tools.subagent import InvokeSubagentTool
from .host.subagents import (
    AgentMetadata,
    append_context_block,
    build_subagent_prompt_block,
)

if TYPE_CHECKING:
    from .service import AgentRuntime


def resolve_host_subagents(
    self: "AgentRuntime",
    host: dict[str, Any] | None,
) -> dict[str, AgentMetadata]:
    """Resolve a host snapshot's subagent ids to pool metadata."""
    if not host:
        return {}
    subagent_ids = [str(value) for value in host.get("subagents") or []]
    if not subagent_ids:
        return {}
    pool = self.get_pool()
    if pool is None:
        raise RuntimeError(
            f"agent '{self.app_id}' received a host snapshot but has no pool attached"
        )
    resolved: dict[str, AgentMetadata] = {}
    for agent_id in subagent_ids:
        metadata = pool.get_agent_metadata(agent_id)
        if metadata is None:
            raise RuntimeError(
                f"host '{host.get('host_id')}' references unknown agent '{agent_id}'"
            )
        resolved[agent_id] = metadata
    return resolved


def resolve_host_system_prompt(
    self: "AgentRuntime",
    host: dict[str, Any] | None,
    context: str | None = None,
) -> SystemPrompt:
    """Render the base prompt with the subagent block and request context.

    The subagent routing block is appended first, then any caller-supplied
    per-request ``context`` after it.
    """
    prompt = build_subagent_prompt_block(
        self.system_prompt,
        resolve_host_subagents(self, host),
    )
    return append_context_block(prompt, context)


def build_agent_instance(
    self: "AgentRuntime",
    *,
    session_id: str,
    shared_llm: Any = None,
    host: dict[str, Any] | None = None,
) -> Agent:
    tools = self.definition.build_tools()
    skills = getattr(self, "skills", None) or self.definition.build_skills()
    llm = shared_llm if shared_llm is not None else self.definition.build_llm()
    if hasattr(self, "agent"):
        config = replace(self.agent.config)
    else:
        config = self.definition.build_agent_config()
    if config.app_id != self.app_id:
        raise ValueError(
            "AgentSpec.get_agent_id() must match build_agent_config().app_id "
            f"(got {self.app_id!r} vs {config.app_id!r})"
        )
    # During AgentRuntime.__init__ the base prompt is not set yet; fall back
    # to the spec config's own prompt.
    base_prompt = getattr(self, "system_prompt", None)
    if base_prompt is not None:
        config.system_prompt = base_prompt
    host_subagents = resolve_host_subagents(self, host)
    if host_subagents:
        config.system_prompt = build_subagent_prompt_block(
            config.system_prompt, host_subagents
        )

    agent = Agent(llm=llm, tools=tools, skills=skills, config=config)
    if config.skills_enabled and skills.list_skills() and "Skill" not in agent.tools:
        agent.tools.register(SkillTool(skills))
    collector = getattr(self, "signal_collector", None)
    if collector is None:
        collector = build_default_signal_collector()
        self.signal_collector = collector
    agent.set_signal_collector(collector)
    chain_renderer = self.get_chain_renderer()
    if chain_renderer is not None:
        agent.set_chain_renderer(chain_renderer)
    if self.definition.enable_runtime_tools():
        configure_runtime_tools(self, agent, session_id=session_id)
    mcp_servers = self.get_mcp_servers()
    if mcp_servers:
        configure_remote_tools(self, agent, mcp_servers)
    web_search = self.definition.build_web_search()
    if web_search is not None:
        configure_web_search_tools(self, agent, web_search)
    if host_subagents and host is not None:
        configure_subagent_tools(self, agent, session_id=session_id, host=host)
    return agent


def configure_runtime_tools(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
) -> None:
    builder = RuntimeToolBuilder(
        store=self.memory_store,
        app_id=self.app_id,
        session_id=session_id,
        event_logger=self.event_logger,
    )
    for tool in builder.build_tools():
        agent.tools.register(tool)


def configure_remote_tools(
    self: "AgentRuntime",
    agent: Agent,
    mcp_servers: Sequence[MCPServerConfig],
) -> None:
    if self.mcp_manager is None:
        self.mcp_manager = MCPManager(
            event_logger=self.event_logger,
            app_id=self.app_id,
        )
        self.has_mcp_manager = True

    manager = self.mcp_manager
    try:
        for server in mcp_servers:
            if manager.get_server(server.name) is None:
                manager.add_server(
                    name=server.name,
                    url=server.url,
                    description=server.description,
                    headers=server.headers,
                    allowed_tools=server.allowed_tools,
                    auto_connect=True,
                )

        mcp_tools = manager.get_flattened_tools(prefix="mcp_")
        for mcp_tool in mcp_tools:
            server_name = mcp_tool.get("metadata", {}).get("server")
            original_name = mcp_tool.get("metadata", {}).get("original_name")
            if not server_name or not original_name:
                continue

            def make_executor(srv_name: str, tool_name: str):
                def executor(args):
                    try:
                        result = manager.call_tool(srv_name, tool_name, args)
                        return extract_mcp_text(result)
                    except Exception as exc:  # pragma: no cover
                        return f"Error: {exc}"

                return executor

            adapter = MCPToolAdapter.from_mcp_tool(
                mcp_tool=mcp_tool,
                executor=make_executor(server_name, original_name),
                prefix="",
            )
            if adapter.name not in agent.tools:
                agent.tools.register(adapter)
    except MCPClientError:
        return


def configure_web_search_tools(
    self: "AgentRuntime",
    agent: Agent,
    provider: Any,
) -> None:
    """Register a web search provider's tools under their plain names.

    Reuses the MCP manager for connection, headers, and call routing, but
    registers tools as e.g. ``web_search`` rather than the
    ``mcp_<server>_<tool>`` names ``configure_remote_tools`` produces.
    """
    server = provider.mcp_server_config()
    if self.mcp_manager is None:
        self.mcp_manager = MCPManager(
            event_logger=self.event_logger,
            app_id=self.app_id,
        )
        self.has_mcp_manager = True

    manager = self.mcp_manager
    try:
        if manager.get_server(server.name) is None:
            manager.add_server(
                name=server.name,
                url=server.url,
                description=server.description,
                headers=server.headers,
                allowed_tools=server.allowed_tools,
                auto_connect=True,
            )

        for mcp_tool in manager.get_flattened_tools(prefix="mcp_"):
            metadata = mcp_tool.get("metadata", {})
            if metadata.get("server") != server.name:
                continue
            original_name = metadata.get("original_name")
            if not original_name:
                continue

            def make_executor(srv_name: str, tool_name: str):
                def executor(args):
                    try:
                        result = manager.call_tool(srv_name, tool_name, args)
                        return extract_mcp_text(result)
                    except Exception as exc:  # pragma: no cover
                        return f"Error: {exc}"

                return executor

            adapter = MCPToolAdapter.from_mcp_tool(
                mcp_tool={**mcp_tool, "name": original_name},
                executor=make_executor(server.name, original_name),
                prefix="",
            )
            if adapter.name not in agent.tools:
                agent.tools.register(adapter)
    except MCPClientError:
        return


def configure_subagent_tools(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
    host: dict[str, Any],
) -> None:
    pool = self.get_pool()
    if pool is None:
        raise RuntimeError(
            f"agent '{self.app_id}' received a host snapshot but has no pool attached"
        )
    host_id = str(host.get("host_id") or "")
    allowed = {str(value) for value in host.get("subagents") or []}

    def client_resolver(agent_id: str) -> Any:
        if agent_id not in allowed:
            raise ValueError(
                f"subagent '{agent_id}' is not in host '{host_id}'"
            )
        return pool.get_client(agent_id)

    if "InvokeSubagent" in agent.tools:
        agent.tools.unregister("InvokeSubagent")
    agent.tools.register(
        InvokeSubagentTool(
            client_resolver=client_resolver,
            primary_app_id=self.app_id,
            primary_session_id=session_id,
            event_logger=self.get_event_logger(),
        )
    )


def extract_mcp_text(result: Any) -> str:
    """Extract plain text output from an MCP tool result payload."""
    if isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return "\n".join(texts) if texts else str(result)
    return str(result)

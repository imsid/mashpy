"""Agent runtime factory and configuration helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Sequence

from mash.mcp.client import MCPClientError
from mash.mcp.manager import MCPManager
from mash.mcp.types import MCPServerConfig
from mash.tools.mcp import MCPToolAdapter

from ..core.agent import Agent
from ..memory.signals import build_default_signal_collector
from ..tools.runtime import RuntimeToolBuilder
from ..tools.subagent import InvokeSubagentTool

if TYPE_CHECKING:
    from .service import AgentRuntime


def build_agent_instance(
    self: "AgentRuntime",
    *,
    session_id: str,
) -> Agent:
    tools = self.definition.build_tools()
    skills = self.definition.build_skills()
    llm = self.definition.build_llm()
    if hasattr(self, "agent"):
        config = replace(self.agent.config)
    else:
        config = self.definition.build_agent_config()
    if config.app_id != self.app_id:
        raise ValueError(
            "AgentSpec.get_agent_id() must match build_agent_config().app_id "
            f"(got {self.app_id!r} vs {config.app_id!r})"
        )
    configured_prompt = getattr(self, "system_prompt", None)
    if configured_prompt is not None:
        config.system_prompt = configured_prompt

    agent = Agent(llm=llm, tools=tools, skills=skills, config=config)
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
    if self.has_subagent_clients():
        configure_subagent_tools(self, agent, session_id=session_id)
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
            default_model=agent.llm.model,
            event_logger=self.event_logger,
            session_id=self.session_id,
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


def configure_subagent_tools(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
) -> None:
    agent.config.system_prompt = self.system_prompt
    if "InvokeSubagent" in agent.tools:
        agent.tools.unregister("InvokeSubagent")
    agent.tools.register(
        InvokeSubagentTool(
            client_resolver=self.get_subagent_client,
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

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
from ..browser.sites import check_sites
from ..tools.browser import browser_tool_names, build_browser_tools
from ..tools.sandbox import SANDBOX_TOOL_NAMES, build_sandbox_tools
from ..tools.runtime import RuntimeToolBuilder
from ..tools.subagent import RunSubAgentsTool
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
    parent_kind: str | None = None,
) -> dict[str, AgentMetadata]:
    """Resolve a host snapshot's subagent ids to pool metadata.

    Only the host's primary routes to subagents. A subagent request carries the
    same snapshot, but must not see its siblings or call them. A request a
    RunSubAgents call started gets none either, even when it runs on the
    primary itself: splitting is one level deep.
    """
    if parent_kind == "subagent":
        return {}
    if not host or host.get("primary") != self.app_id:
        return {}
    subagent_ids = [str(value) for value in host.get("subagents") or []]
    if not subagent_ids:
        return {}
    access = self.get_subagent_access()
    if access is None:
        raise RuntimeError(
            f"agent '{self.app_id}' received a host snapshot but has no subagent access"
        )
    resolved: dict[str, AgentMetadata] = {}
    for agent_id in subagent_ids:
        metadata = access.get_agent_metadata(agent_id)
        if metadata is None:
            raise RuntimeError(
                f"host '{host.get('host_id')}' references unknown agent '{agent_id}'"
            )
        resolved[agent_id] = metadata
    return resolved


def _with_directory(
    self: "AgentRuntime",
    base_prompt: SystemPrompt,
    subagents: dict[str, AgentMetadata],
) -> SystemPrompt:
    """Append the RunSubAgents directory, listing this agent first."""
    if not subagents:
        return base_prompt
    access = self.get_subagent_access()
    own_metadata = access.get_agent_metadata(self.app_id) if access is not None else None
    return build_subagent_prompt_block(
        base_prompt,
        subagents,
        own_agent_id=self.app_id,
        own_metadata=own_metadata,
    )


def resolve_host_system_prompt(
    self: "AgentRuntime",
    host: dict[str, Any] | None,
    context: str | None = None,
    *,
    parent_kind: str | None = None,
) -> SystemPrompt:
    """Render the base prompt with the subagent block and request context.

    The subagent routing block is appended first, then any caller-supplied
    per-request ``context`` after it.
    """
    prompt = _with_directory(
        self, self.system_prompt, resolve_host_subagents(self, host, parent_kind)
    )
    return append_context_block(prompt, context)


def register_sandbox_tools(self: "AgentRuntime", agent: Agent) -> None:
    """Register the sandbox tools, when the host has sandboxes."""
    if self._sandbox_tools is None:
        return
    for name in SANDBOX_TOOL_NAMES:
        if name in agent.tools:
            raise ValueError(
                f"agent '{self.app_id}' registers its own '{name}' tool; the host "
                "provides it on the request user's sandbox, so remove it from build_tools()"
            )
    host_api_url, token = self._sandbox_tools
    for tool in build_sandbox_tools(
        host_api_url=host_api_url,
        token=token,
        agent_id=self.app_id,
        file_roots=self.definition.sandbox_file_roots(),
        env=self.definition.sandbox_env(),
    ):
        agent.tools.register(tool)


def register_browser_tools(self: "AgentRuntime", agent: Agent) -> None:
    """Register the browser tools, when the host has browsers and the spec lists sites."""
    if self._browser_tools is None:
        return
    for name in browser_tool_names():
        if name in agent.tools:
            raise ValueError(
                f"agent '{self.app_id}' registers its own '{name}' tool; the host "
                "provides it on the request user's browser, so remove it from build_tools()"
            )
    try:
        check_sites(self.definition.browser_sites())
    except ValueError as exc:
        raise ValueError(f"agent '{self.app_id}': {exc}") from exc
    host_api_url, token = self._browser_tools
    for tool in build_browser_tools(host_api_url=host_api_url, token=token):
        agent.tools.register(tool)


def build_agent_instance(
    self: "AgentRuntime",
    *,
    session_id: str,
    shared_llm: Any = None,
    host: dict[str, Any] | None = None,
    user_id: str | None = None,
    parent_kind: str | None = None,
    for_request: bool = False,
) -> Agent:
    """Build an agent; ``for_request`` builds one for a request's turn.

    A request's agent gets ``RunSubAgents`` unless a ``RunSubAgents`` call
    started the request, in which case it gets neither ``RunSubAgents`` nor
    ``AskUser``: the user talks to the calling agent, not its children.
    """
    tools = self.definition.build_tools()
    skills = getattr(self, "skills", None) or self.definition.build_skills()
    llm = shared_llm if shared_llm is not None else self.definition.build_llm()
    llm.enable_thought_summaries()
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
    host_subagents = resolve_host_subagents(self, host, parent_kind)
    if host_subagents:
        config.system_prompt = _with_directory(self, config.system_prompt, host_subagents)

    agent = Agent(llm=llm, tools=tools, skills=skills, config=config)
    register_sandbox_tools(self, agent)
    register_browser_tools(self, agent)
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
        configure_runtime_tools(self, agent, session_id=session_id, user_id=user_id)
    cortex = self.get_cortex()
    if cortex is not None and user_id:
        agent.tools.register(cortex.search_tool(user_id))
    mcp_servers = self.get_mcp_servers()
    if mcp_servers:
        configure_remote_tools(self, agent, mcp_servers)
    web_search = self.definition.build_web_search()
    if web_search is not None:
        configure_web_search_tools(self, agent, web_search)
    if for_request:
        if parent_kind == "subagent":
            for name in ("RunSubAgents", "AskUser"):
                if name in agent.tools:
                    agent.tools.unregister(name)
        else:
            configure_run_subagents(
                self,
                agent,
                session_id=session_id,
                host=host,
                subagent_ids=set(host_subagents),
            )
    return agent


def configure_runtime_tools(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
    user_id: str | None = None,
) -> None:
    builder = RuntimeToolBuilder(
        store=self.memory_store,
        app_id=self.app_id,
        session_id=session_id,
        user_id=user_id,
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
                        return f"Error: {exc}", True

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
                        return f"Error: {exc}", True

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


def configure_run_subagents(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
    host: dict[str, Any] | None,
    subagent_ids: set[str],
) -> None:
    """Register ``RunSubAgents`` for a request the user started.

    Its children may run on this agent or on the host's subagents. Without
    subagent access (a runtime outside a host) there is no route to any
    child, so the tool is not registered.
    """
    access = self.get_subagent_access()
    if access is None:
        if subagent_ids:
            raise RuntimeError(
                f"agent '{self.app_id}' received a host snapshot but has no subagent access"
            )
        return
    host_id = str((host or {}).get("host_id") or "")
    allowed = {self.app_id, *subagent_ids}

    def client_resolver(agent_id: str) -> Any:
        if agent_id not in allowed:
            if host_id:
                raise ValueError(f"agent '{agent_id}' is not in host '{host_id}'")
            raise ValueError(
                f"agent '{agent_id}' is not available; without a host, "
                f"RunSubAgents runs only on '{self.app_id}'"
            )
        return access.get_client(agent_id)

    if "RunSubAgents" in agent.tools:
        agent.tools.unregister("RunSubAgents")
    agent.tools.register(
        RunSubAgentsTool(
            client_resolver=client_resolver,
            caller_agent_id=self.app_id,
            session_id=session_id,
            host=host,
        )
    )


def extract_mcp_text(result: Any) -> tuple[str, bool]:
    """Extract text output and the error flag from an MCP tool result payload.

    Returns a ``(text, is_error)`` tuple. ``is_error`` mirrors the MCP
    ``isError`` field so an error the server reports is surfaced as a failed
    tool result rather than silently recorded as a success.
    """
    if isinstance(result, dict):
        is_error = bool(result.get("isError", False))
        content = result.get("content", [])
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return ("\n".join(texts) if texts else str(result)), is_error
        return str(result), is_error
    return str(result), False

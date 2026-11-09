"""Agent runtime factory and configuration helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mash.mcp.manager import MCPManager
from mash.tools.mcp import MCPToolAdapter
from mash.skills.tool import SkillTool

from ..core.agent import Agent
from ..core.config import SystemPrompt
from ..browser.sites import check_sites
from ..tools.browser import browser_tool_names, build_browser_tools
from ..tools.sandbox import SANDBOX_TOOL_NAMES, build_sandbox_tools
from ..tools.runtime import RuntimeToolBuilder
from ..tools.subagent import RunSubAgentsTool
from ..tools.workflow import RunWorkflowTool
from .host.directory import (
    AgentMetadata,
    append_context_block,
    build_agent_directory,
)

if TYPE_CHECKING:
    from .service import AgentRuntime


def _is_companion(self: "AgentRuntime") -> bool:
    from mash.agents.companion import COMPANION_AGENT_ID

    return self.app_id == COMPANION_AGENT_ID


def resolve_host_agents(
    self: "AgentRuntime",
    host: dict[str, Any] | None,
    parent_kind: str | None = None,
) -> dict[str, AgentMetadata]:
    """Resolve a host snapshot's agent ids to pool metadata.

    Only the companion routes, on a request the user started; a request a
    RunSubAgents call started gets no agents, so routing is one level deep.
    """
    if parent_kind == "subagent" or not host or not _is_companion(self):
        return {}
    agent_ids = [str(value) for value in host.get("agents") or []]
    if not agent_ids:
        return {}
    access = self.get_host_access()
    if access is None:
        raise RuntimeError(
            f"agent '{self.app_id}' received a host snapshot but has no host access"
        )
    resolved: dict[str, AgentMetadata] = {}
    for agent_id in agent_ids:
        metadata = access.get_agent_metadata(agent_id)
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
    *,
    parent_kind: str | None = None,
) -> SystemPrompt:
    """Render the base prompt with the agent directory and request context.

    The directory is appended first, then any caller-supplied per-request
    ``context`` after it.
    """
    prompt = build_agent_directory(
        self.system_prompt, resolve_host_agents(self, host, parent_kind)
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


def build_base_agent(self: "AgentRuntime") -> Agent:
    """The agent the runtime builds once, when it is created.

    Its tools are the spec's ``build_tools()``, the ``Skill`` tool, and later
    the sandbox and browser tools (``enable_sandbox_tools``,
    ``enable_browser_tools``) and the MCP and web search tools (``open()``).
    They live for the process and serve every request at once; the runtime
    shuts them down when it shuts down. Tools bound to one request are added
    per step by :func:`build_turn_agent`.
    """
    tools = self.definition.build_tools()
    skills = self.definition.build_skills()
    llm = self.definition.build_llm()
    llm.enable_thought_summaries()
    config = self.definition.build_agent_config()
    if config.app_id != self.app_id:
        raise ValueError(
            "AgentSpec.get_agent_id() must match build_agent_config().app_id "
            f"(got {self.app_id!r} vs {config.app_id!r})"
        )
    agent = Agent(llm=llm, tools=tools, skills=skills, config=config)
    if config.skills_enabled and skills.list_skills() and "Skill" not in agent.tools:
        agent.tools.register(SkillTool(skills))
    agent.set_signal_collector(self.signal_collector)
    return agent


def build_turn_agent(
    self: "AgentRuntime",
    *,
    session_id: str,
    host: dict[str, Any] | None = None,
    user_id: str | None = None,
    parent_kind: str | None = None,
) -> Agent:
    """An agent for one step of a request, over the runtime's shared tools.

    It shares the base agent's LLM, skills and tool objects, and adds the
    tools bound to the request: the conversation tools for its session and
    user, and ``search_cortex`` for its user. The companion, on a host request
    the user started, also gets ``RunSubAgents`` over the host's agents and
    ``RunWorkflow`` over its workflows.
    """
    base = self.agent
    config = replace(base.config)
    config.system_prompt = self.system_prompt
    host_agents = resolve_host_agents(self, host, parent_kind)
    config.system_prompt = build_agent_directory(config.system_prompt, host_agents)

    agent = Agent(llm=base.llm, tools=base.tools.copy(), skills=base.skills, config=config)
    agent.set_signal_collector(self.signal_collector)
    chain_renderer = self.get_chain_renderer()
    if chain_renderer is not None:
        agent.set_chain_renderer(chain_renderer)
    if self.definition.enable_runtime_tools():
        configure_runtime_tools(self, agent, session_id=session_id, user_id=user_id)
    cortex = self.get_cortex()
    if cortex is not None and user_id:
        agent.tools.register(cortex.search_tool(user_id))
    for name in ("RunSubAgents", "RunWorkflow"):
        if name in agent.tools:
            agent.tools.unregister(name)
    if parent_kind != "subagent":
        configure_run_subagents(self, agent, session_id=session_id, host=host, agents=host_agents)
        configure_run_workflow(self, agent, host=host)
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


def register_mcp_tools(self: "AgentRuntime", agent: Agent) -> None:
    """Register the tools of the MCP servers and the web search provider.

    The tools come from the lists ``AgentRuntime.open()`` fetched, so building
    an agent makes no network call. An MCP server's tools are named
    ``mcp_<server>_<tool>``; the web search provider's keep their plain names
    (``web_search``, ``web_fetch``). A server that failed to connect has none.
    """
    manager = self.mcp_manager
    if manager is None:
        return
    web_search = self.get_web_search_server()
    for server_name, tools in manager.get_all_tools().items():
        plain = web_search is not None and server_name == web_search.name
        for mcp_tool in tools:
            original_name = mcp_tool["metadata"]["original_name"]
            adapter = MCPToolAdapter.from_mcp_tool(
                mcp_tool={**mcp_tool, "name": original_name} if plain else mcp_tool,
                executor=_mcp_executor(manager, server_name, original_name),
            )
            if adapter.name not in agent.tools:
                agent.tools.register(adapter)


def _mcp_executor(manager: MCPManager, server_name: str, tool_name: str):
    async def executor(args: dict[str, Any]) -> tuple[str, bool]:
        return extract_mcp_text(await manager.call_tool(server_name, tool_name, args))

    return executor


def configure_run_subagents(
    self: "AgentRuntime",
    agent: Agent,
    *,
    session_id: str,
    host: dict[str, Any] | None,
    agents: dict[str, AgentMetadata],
) -> None:
    """Register ``RunSubAgents`` for the companion, over the host's agents."""
    if not agents:
        return
    access = self.get_host_access()
    host_id = str((host or {}).get("host_id") or "")

    def client_resolver(agent_id: str) -> Any:
        if agent_id not in agents:
            raise ValueError(f"agent '{agent_id}' is not in host '{host_id}'")
        return access.get_client(agent_id)

    agent.tools.register(
        RunSubAgentsTool(
            client_resolver=client_resolver,
            caller_agent_id=self.app_id,
            session_id=session_id,
            host=host,
            timeout_seconds=agent.config.subagent_timeout_seconds,
        )
    )


def configure_run_workflow(
    self: "AgentRuntime", agent: Agent, *, host: dict[str, Any] | None
) -> None:
    """Register ``RunWorkflow`` for the companion when its host has workflows."""
    workflow_ids = [str(value) for value in (host or {}).get("workflows") or []]
    if not _is_companion(self) or not workflow_ids:
        return
    access = self.get_host_access()
    get_workflow_client = getattr(access, "get_workflow_client", None)
    if get_workflow_client is None:
        return
    agent.tools.register(
        RunWorkflowTool(relay=get_workflow_client(), workflow_ids=workflow_ids)
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

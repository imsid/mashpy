"""Agent pool managing in-process per-agent runtime servers and clients."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from mash.core.database import resolve_database_url
from mash.memory.store import MemoryStore, PostgresStore
from mash.skills.base import Skill
from mash.skills.tool import SkillTool
from mash.workflows import WorkflowRegistry, WorkflowService, WorkflowSpec
from mash.workflows.dbos import make_runner_id
from mash.workflows.dbos import register_runner as register_workflow_runner
from mash.workflows.dbos import unregister_runner as unregister_workflow_runner

from ..client import AgentClientLike, InProcessAgentClient
from ..events import PostgresRuntimeStore, RuntimeStore
from ..service import AgentRuntime
from ..spec import AgentSpec
from .subagents import AgentMetadata
from .types import AgentRegistration, Host


class AgentPool:
    """Deployed pool of role-less agents and the hosts composed over them.

    The pool is the unit of deploy; a :class:`Host` is the unit of
    composition. Hosts are in-memory values: define them in code at build
    time or over the control API, and re-define them after a restart.
    """

    def __init__(
        self,
        *,
        runtime_database_url: str | None = None,
    ) -> None:
        self.runner_id = make_runner_id()
        self.runtime_database_url = str(runtime_database_url or "").strip() or None
        self._registered: Dict[str, AgentRegistration] = {}
        self._hosts: Dict[str, Host] = {}
        self._agents: Dict[str, AgentRuntime] = {}
        self._clients: Dict[str, AgentClientLike] = {}
        self._agent_skills: Dict[str, Dict[str, Skill]] = {}
        self._agent_workflows: Dict[str, set[str]] = {}
        self._shared_runtime_store: RuntimeStore | None = None
        self._shared_memory_store: MemoryStore | None = None
        self._workflow_registry = WorkflowRegistry()
        self._workflow_service = WorkflowService(
            self._workflow_registry,
            self,
            runner_id=self.runner_id,
        )

    def configure_runtime_database_url(self, database_url: str | None) -> None:
        value = str(database_url or "").strip()
        self.runtime_database_url = value or None

    def register_agent(
        self,
        definition: AgentSpec,
        *,
        metadata: AgentMetadata,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")
        if metadata is None:
            raise ValueError(f"agent '{resolved_agent_id}' metadata is required")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=metadata,
        )
        return resolved_agent_id

    def register_workflow_agent(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_workflow_agent=True,
        )
        return resolved_agent_id

    def define_host(self, host: Host) -> Host:
        for agent_id in (host.primary, *host.subagents):
            registered = self._registered.get(agent_id)
            if registered is None or registered.is_workflow_agent:
                raise ValueError(
                    f"host '{host.host_id}' references unknown agent '{agent_id}'"
                )
        for workflow_id in host.workflows:
            try:
                self._workflow_registry.get(workflow_id)
            except KeyError:
                raise ValueError(
                    f"host '{host.host_id}' references unknown workflow "
                    f"'{workflow_id}'"
                ) from None
        self._hosts[host.host_id] = host
        return host

    def get_host(self, host_id: str) -> Host:
        host = self._hosts.get(str(host_id or "").strip())
        if host is None:
            raise ValueError(f"host '{host_id}' is not defined")
        return host

    def list_hosts(self) -> list[Host]:
        return list(self._hosts.values())

    def describe_host(self, host_id: str) -> dict[str, Any]:
        host = self.get_host(host_id)

        def _member(agent_id: str) -> dict[str, Any]:
            metadata = self.get_agent_metadata(agent_id)
            return {
                "agent_id": agent_id,
                "metadata": asdict(metadata) if metadata is not None else None,
            }

        return {
            "host_id": host.host_id,
            "primary": _member(host.primary),
            "subagents": [_member(agent_id) for agent_id in host.subagents],
            "workflows": list(host.workflows),
        }

    def snapshot_for(self, host: Host) -> dict[str, Any]:
        return {
            "host_id": host.host_id,
            "primary": host.primary,
            "subagents": list(host.subagents),
        }

    async def submit_host_request(
        self,
        host_id: str,
        *,
        message: str,
        session_id: str,
        structured_output: Any = None,
    ) -> dict[str, Any]:
        host = self.get_host(host_id)
        runtime = self.get_agent(host.primary)
        return await runtime.submit_request(
            message=message,
            session_id=session_id,
            structured_output=structured_output,
            host_snapshot=self.snapshot_for(host),
        )

    def register_workflow(self, workflow: WorkflowSpec) -> None:
        self._ensure_workflow_task_agents(workflow)
        self._workflow_registry.register(workflow)

    def register_agent_workflow(self, agent_id: str, workflow: WorkflowSpec) -> None:
        resolved_agent_id = str(agent_id or "").strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        self._require_registered_agent(resolved_agent_id)
        self._ensure_workflow_task_agents(workflow)
        self._workflow_registry.upsert(workflow)
        self._remove_agent_workflow(workflow.workflow_id)
        self._agent_workflows.setdefault(resolved_agent_id, set()).add(workflow.workflow_id)

    def unregister_agent_workflow(self, agent_id: str, workflow_id: str) -> None:
        resolved_agent_id = str(agent_id or "").strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        self._require_registered_agent(resolved_agent_id)
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        owner_agent_id = self._workflow_owner_agent_id(resolved_workflow_id)
        if owner_agent_id is not None and owner_agent_id != resolved_agent_id:
            raise ValueError(
                f"workflow '{resolved_workflow_id}' is registered for agent "
                f"'{owner_agent_id}'"
            )
        self._workflow_registry.unregister(resolved_workflow_id)
        self._remove_agent_workflow(resolved_workflow_id)

    def register_agent_skill(self, agent_id: str, skill: Skill) -> None:
        resolved_agent_id = str(agent_id or "").strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        self._require_registered_agent(resolved_agent_id)

        agent_skills = self._agent_skills.get(resolved_agent_id, {})
        if skill.name in agent_skills:
            raise ValueError(
                f"Skill '{skill.name}' is already registered for agent '{resolved_agent_id}'"
            )

        runtime = self._agents.get(resolved_agent_id)
        if runtime is not None:
            self._register_runtime_skill(runtime, skill)
        agent_skills = self._agent_skills.setdefault(resolved_agent_id, {})
        agent_skills[skill.name] = skill

    def unregister_agent_skill(self, agent_id: str, skill_name: str) -> None:
        resolved_agent_id = str(agent_id or "").strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        self._require_registered_agent(resolved_agent_id)
        resolved_skill_name = str(skill_name or "").strip()
        if not resolved_skill_name:
            raise ValueError("skill_name is required")

        agent_skills = self._agent_skills.get(resolved_agent_id)
        if agent_skills is not None:
            agent_skills.pop(resolved_skill_name, None)
            if not agent_skills:
                self._agent_skills.pop(resolved_agent_id, None)

        runtime = self._agents.get(resolved_agent_id)
        if runtime is not None:
            runtime.skills.unregister(resolved_skill_name)
            self._refresh_runtime_skill_tool(runtime)

    def get_registered_agent_spec(self, agent_id: str) -> AgentSpec | None:
        registered = self._registered.get(str(agent_id or "").strip())
        return registered.definition if registered is not None else None

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None:
        registered = self._registered.get(str(agent_id or "").strip())
        return registered.metadata if registered is not None else None

    async def start(self) -> None:
        if self._clients:
            return
        if not self.runtime_database_url:
            self.runtime_database_url = resolve_database_url()
        if not self.runtime_database_url:
            raise RuntimeError("MASH_DATABASE_URL is required to start hosted Mash runtimes")
        register_workflow_runner(self.runner_id, self)
        try:
            shared_runtime_store = PostgresRuntimeStore(self.runtime_database_url)
            shared_memory_store = PostgresStore(self.runtime_database_url)
            self._shared_runtime_store = shared_runtime_store
            self._shared_memory_store = shared_memory_store
            await shared_runtime_store.open()
            await shared_memory_store.open()

            for registered in self._registered.values():
                uses_default_memory = (
                    type(registered.definition).build_memory_store
                    is AgentSpec.build_memory_store
                )
                memory_store: MemoryStore = (
                    shared_memory_store
                    if uses_default_memory
                    else registered.definition.build_memory_store()
                )
                runtime = AgentRuntime.from_spec(
                    registered.definition,
                    runtime_database_url=self.runtime_database_url,
                    session_id=registered.session_id,
                    runtime_store=shared_runtime_store,
                    memory_store=memory_store,
                )
                runtime.attach_pool(self)
                agent_skills = self._agent_skills.get(registered.agent_id, {})
                for skill in agent_skills.values():
                    self._register_runtime_skill(runtime, skill)
                self._agents[registered.agent_id] = runtime

            for agent_id, runtime in self._agents.items():
                await runtime.open()
                self._clients[agent_id] = InProcessAgentClient(runtime)
        except Exception:
            await self.close()
            raise

    def get_client(self, agent_id: str) -> AgentClientLike:
        client = self._clients.get(agent_id)
        if client is None:
            raise ValueError(f"agent client '{agent_id}' is not registered")
        return client

    def get_agent(self, agent_id: str) -> AgentRuntime:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise ValueError(f"agent '{agent_id}' is not registered")
        return agent

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._workflow_registry

    def get_workflow_service(self) -> WorkflowService:
        return self._workflow_service

    def list_agents(self) -> list[str]:
        return [
            agent_id
            for agent_id, registered in self._registered.items()
            if not registered.is_workflow_agent
        ]

    def describe_agents(self) -> list[dict[str, object]]:
        described: list[dict[str, object]] = []
        for registered in self._registered.values():
            if registered.is_workflow_agent:
                continue
            described.append(
                {
                    "agent_id": registered.agent_id,
                    "metadata": (
                        asdict(registered.metadata)
                        if registered.metadata is not None
                        else None
                    ),
                }
            )
        return described

    def describe_hosts(self) -> list[dict[str, Any]]:
        return [
            {
                "host_id": host.host_id,
                "primary": host.primary,
                "subagents": list(host.subagents),
                "workflows": list(host.workflows),
            }
            for host in self._hosts.values()
        ]

    def describe_tools(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        agents_by_tool: dict[str, list[str]] = {}
        for agent_id, runtime in self._agents.items():
            for name in runtime.tools.list_tools():
                tool = runtime.tools.get(name)
                if tool is None:
                    continue
                if name not in seen:
                    seen[name] = {
                        "name": tool.name,
                        "description": getattr(tool, "description", ""),
                        "parameters": getattr(tool, "parameters", {}),
                        "requires_approval": getattr(tool, "requires_approval", False),
                        "parallel_safe": getattr(tool, "parallel_safe", True),
                    }
                agents_by_tool.setdefault(name, []).append(agent_id)
        return [{"tool": seen[n], "agents": agents_by_tool[n]} for n in seen]

    def describe_skills(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        agents_by_skill: dict[str, list[str]] = {}
        for agent_id, runtime in self._agents.items():
            for skill in runtime.skills.list_skills():
                if skill.name not in seen:
                    seen[skill.name] = {
                        "name": skill.name,
                        "description": skill.description,
                        "type": skill.type,
                        "content": skill.content,
                    }
                agents_by_skill.setdefault(skill.name, []).append(agent_id)
        return [{"skill": seen[n], "agents": agents_by_skill[n]} for n in seen]

    async def aggregate_tool_invocations(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._shared_runtime_store is None:
            return []
        counts: dict[str, dict[str, Any]] = {}
        for agent_id in self.list_agents():
            rows = await self._shared_runtime_store.count_tool_invocations(
                agent_id, from_ts=from_ts, to_ts=to_ts
            )
            for row in rows:
                tool_name = row["tool_name"]
                n = row["count"]
                if tool_name not in counts:
                    counts[tool_name] = {"total": 0, "by_agent": {}}
                counts[tool_name]["total"] += n
                counts[tool_name]["by_agent"][agent_id] = n
        return [{"tool_name": name, **data} for name, data in counts.items()]

    async def aggregate_skill_invocations(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._shared_runtime_store is None:
            return []
        counts: dict[str, dict[str, Any]] = {}
        for agent_id in self.list_agents():
            rows = await self._shared_runtime_store.count_skill_invocations(
                agent_id, from_ts=from_ts, to_ts=to_ts
            )
            for row in rows:
                skill_name = row["skill_name"]
                n = row["count"]
                if skill_name not in counts:
                    counts[skill_name] = {"total": 0, "by_agent": {}}
                counts[skill_name]["total"] += n
                counts[skill_name]["by_agent"][agent_id] = n
        return [{"skill_name": name, **data} for name, data in counts.items()]

    async def close(self) -> None:
        unregister_workflow_runner(self.runner_id, self)
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

        for agent in self._agents.values():
            await agent.shutdown()
        self._agents.clear()

        if self._shared_runtime_store is not None:
            await self._shared_runtime_store.close()
            self._shared_runtime_store = None
        if self._shared_memory_store is not None:
            await self._shared_memory_store.close()
            self._shared_memory_store = None

    def _ensure_workflow_task_agents(self, workflow: WorkflowSpec) -> None:
        for task in workflow.tasks:
            agent_id = task.agent_id.strip()
            if not agent_id:
                raise ValueError("workflow task agent id is required")
            existing = self.get_registered_agent_spec(agent_id)
            if existing is None:
                if task.agent_spec is None:
                    raise ValueError(
                        f"workflow task agent '{agent_id}' is not registered"
                    )
                self.register_workflow_agent(task.agent_spec, agent_id=agent_id)
            elif task.agent_spec is not None and existing is not task.agent_spec:
                raise ValueError(
                    f"workflow task agent '{agent_id}' is already registered "
                    "with a different spec"
                )

    def _require_registered_agent(self, agent_id: str) -> None:
        if agent_id not in self._registered:
            raise ValueError(f"agent '{agent_id}' is not registered")

    def _workflow_owner_agent_id(self, workflow_id: str) -> str | None:
        for agent_id, workflow_ids in self._agent_workflows.items():
            if workflow_id in workflow_ids:
                return agent_id
        return None

    def _remove_agent_workflow(self, workflow_id: str) -> None:
        for agent_id, workflow_ids in list(self._agent_workflows.items()):
            workflow_ids.discard(workflow_id)
            if not workflow_ids:
                self._agent_workflows.pop(agent_id, None)

    def _register_runtime_skill(self, runtime: AgentRuntime, skill: Skill) -> None:
        runtime.skills.register(skill)
        self._refresh_runtime_skill_tool(runtime)

    def _refresh_runtime_skill_tool(self, runtime: AgentRuntime) -> None:
        if "Skill" in runtime.agent.tools:
            runtime.agent.tools.unregister("Skill")
        if runtime.skills.list_skills():
            runtime.agent.tools.register(SkillTool(runtime.skills))
        runtime.tools = runtime.agent.tools
        runtime.skills = runtime.agent.skills

    async def __aenter__(self) -> "AgentPool":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.close()


__all__ = ["AgentPool"]

"""Host for managing in-process per-agent runtime servers and clients."""

from __future__ import annotations

from dataclasses import asdict
from typing import Dict, Optional

from mash.core.database import resolve_database_url
from mash.skills.base import Skill
from mash.skills.tool import SkillTool
from mash.workflows import WorkflowRegistry, WorkflowService, WorkflowSpec
from mash.workflows.dbos import make_host_id
from mash.workflows.dbos import register_host as register_workflow_host
from mash.workflows.dbos import unregister_host as unregister_workflow_host

from ..client import AgentClientLike, InProcessAgentClient
from ..factory import configure_subagent_tools
from ..service import AgentRuntime
from ..spec import AgentSpec
from .subagents import SubAgentMetadata, build_subagent_prompt_block
from .types import AgentRegistration


class AgentHost:
    """Host application managing in-process runtimes and per-agent clients."""

    def __init__(
        self,
        *,
        runtime_database_url: str | None = None,
    ) -> None:
        self.host_id = make_host_id()
        self.runtime_database_url = str(runtime_database_url or "").strip() or None
        self._primary_agent_id: Optional[str] = None
        self._registered: Dict[str, AgentRegistration] = {}
        self._agents: Dict[str, AgentRuntime] = {}
        self._clients: Dict[str, AgentClientLike] = {}
        self._agent_skills: Dict[str, Dict[str, Skill]] = {}
        self._agent_workflows: Dict[str, set[str]] = {}
        self._workflow_registry = WorkflowRegistry()
        self._workflow_service = WorkflowService(
            self._workflow_registry,
            self,
            host_id=self.host_id,
        )

    def configure_runtime_database_url(self, database_url: str | None) -> None:
        value = str(database_url or "").strip()
        self.runtime_database_url = value or None

    def register_primary(
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
        if self._primary_agent_id is not None:
            raise ValueError("primary agent is already registered")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
        )
        self._primary_agent_id = resolved_agent_id
        return resolved_agent_id

    def register_subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
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
            metadata=metadata,
            is_primary=False,
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
            is_primary=False,
            is_workflow_agent=True,
        )
        return resolved_agent_id

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

    async def start(self) -> None:
        if self._clients:
            return
        if not self.runtime_database_url:
            self.runtime_database_url = resolve_database_url()
        if not self.runtime_database_url:
            raise RuntimeError("MASH_DATABASE_URL is required to start hosted Mash runtimes")
        register_workflow_host(self.host_id, self)
        try:
            for registered in self._registered.values():
                runtime = AgentRuntime.from_spec(
                    registered.definition,
                    runtime_database_url=self.runtime_database_url,
                    session_id=registered.session_id,
                )
                agent_skills = self._agent_skills.get(registered.agent_id, {})
                for skill in agent_skills.values():
                    self._register_runtime_skill(runtime, skill)
                self._agents[registered.agent_id] = runtime

            if self._primary_agent_id is not None:
                primary = self._agents[self._primary_agent_id]
                subagent_metadata = {
                    registered.agent_id: registered.metadata
                    for registered in self._registered.values()
                    if (
                        not registered.is_primary
                        and not registered.is_workflow_agent
                        and registered.metadata is not None
                    )
                }
                primary.set_subagent_ids(sorted(subagent_metadata.keys()))
                if subagent_metadata:
                    primary.set_subagent_clients(
                        {
                            agent_id: InProcessAgentClient(self._agents[agent_id])
                            for agent_id in sorted(subagent_metadata.keys())
                        }
                    )
                    primary.set_system_prompt(
                        build_subagent_prompt_block(
                            primary.system_prompt,
                            subagent_metadata,
                        )
                    )
                    configure_subagent_tools(
                        primary,
                        primary.agent,
                        session_id=primary.session_id,
                    )

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

    def get_primary_agent_id(self) -> str:
        if self._primary_agent_id is None:
            raise ValueError("primary agent is not registered")
        return self._primary_agent_id

    def describe_agents(self) -> list[dict[str, object]]:
        described: list[dict[str, object]] = []
        for registered in self._registered.values():
            if registered.is_workflow_agent:
                continue
            described.append(
                {
                    "agent_id": registered.agent_id,
                    "role": "primary" if registered.is_primary else "subagent",
                    "metadata": (
                        asdict(registered.metadata)
                        if registered.metadata is not None
                        else None
                    ),
                }
            )
        return described

    async def close(self) -> None:
        unregister_workflow_host(self.host_id, self)
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

        for agent in self._agents.values():
            await agent.shutdown()
        self._agents.clear()

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

    async def __aenter__(self) -> "AgentHost":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.close()


__all__ = ["AgentHost"]

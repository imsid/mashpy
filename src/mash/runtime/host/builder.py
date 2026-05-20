"""Builder for host composition."""

from __future__ import annotations

import importlib

from mash.core.database import resolve_database_url
from mash.workflows import WorkflowSpec

from ..spec import AgentSpec
from .host import AgentHost
from .subagents import SubAgentMetadata
from .types import AgentRegistration


def load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.MASHER_AGENT_ID,
        masher.build_masher_workflow_specs,
        masher.create_masher_agent_spec,
    )


class HostBuilder:
    """Builder for composing one primary runtime and optional subagents."""

    def __init__(self) -> None:
        self._primary: AgentRegistration | None = None
        self._subagents: list[AgentRegistration] = []
        self._masher_enabled = False
        self._workflows: list[WorkflowSpec] = []

    def primary(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        if self._primary is not None:
            raise ValueError("primary agent is already configured")
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._primary = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_primary=True,
        )
        return self

    def enable_masher(self, enabled: bool = True) -> "HostBuilder":
        self._masher_enabled = bool(enabled)
        return self

    def subagent(
        self,
        definition: AgentSpec,
        *,
        metadata: SubAgentMetadata,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._subagents.append(
            AgentRegistration(
                agent_id=resolved_agent_id,
                definition=definition,
                metadata=metadata,
                is_primary=False,
            )
        )
        return self

    def workflow(self, workflow: WorkflowSpec) -> "HostBuilder":
        self._workflows.append(workflow)
        return self

    def build(self) -> AgentHost:
        if self._primary is None:
            raise ValueError("primary agent is required")

        host = AgentHost(runtime_database_url=resolve_database_url())
        host.register_primary(
            self._primary.definition,
            agent_id=self._primary.agent_id,
        )
        for registered in self._subagents:
            metadata = registered.metadata
            if metadata is None:
                raise ValueError(
                    f"subagent '{registered.agent_id}' is missing metadata"
                )
            host.register_subagent(
                registered.definition,
                metadata=metadata,
                agent_id=registered.agent_id,
            )
        if self._masher_enabled:
            (
                masher_agent_id,
                build_masher_workflow_specs,
                create_masher_agent_spec,
            ) = load_masher_components()
            masher_spec = create_masher_agent_spec(target_app_id=self._primary.agent_id)
            host.register_workflow_agent(masher_spec, agent_id=masher_agent_id)
            for workflow in build_masher_workflow_specs(masher_spec):
                host.register_workflow(workflow)
        for workflow in self._workflows:
            for task in workflow.tasks:
                agent_id = task.agent_id.strip()
                if not agent_id:
                    raise ValueError("workflow task agent id is required")
                existing = host.get_registered_agent_spec(agent_id)
                if existing is None:
                    host.register_workflow_agent(task.agent_spec, agent_id=agent_id)
                elif existing is not task.agent_spec:
                    raise ValueError(
                        f"workflow task agent '{agent_id}' is already registered with a different spec"
                    )
            host.register_workflow(workflow)
        return host

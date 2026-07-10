"""Builder for pool composition."""

from __future__ import annotations

import importlib

from mash.core.database import resolve_database_url
from mash.workflows import WorkflowSpec

from ..spec import AgentSpec
from .host import AgentPool
from .subagents import AgentMetadata
from .types import AgentRegistration, Host


def load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.EVAL_AGENT_ID,
        masher.EvalAgentSpec,
        masher.build_eval_agent_metadata,
        masher.build_masher_workflows,
    )


class HostBuilder:
    """Builder composing a flat agent pool, workflows, and host definitions."""

    def __init__(self) -> None:
        self._agents: list[AgentRegistration] = []
        self._hosts: list[Host] = []
        self._workflows: list[WorkflowSpec] = []

    def agent(
        self,
        definition: AgentSpec,
        *,
        metadata: AgentMetadata,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._agents.append(
            AgentRegistration(
                agent_id=resolved_agent_id,
                definition=definition,
                metadata=metadata,
            )
        )
        return self

    def host(self, host: Host) -> "HostBuilder":
        self._hosts.append(host)
        return self

    def workflow(self, workflow: WorkflowSpec) -> "HostBuilder":
        self._workflows.append(workflow)
        return self

    def build(self) -> AgentPool:
        pool = AgentPool(runtime_database_url=resolve_database_url())
        for registered in self._agents:
            pool.register_agent(
                registered.definition,
                metadata=registered.metadata,
                agent_id=registered.agent_id,
            )
        (
            eval_agent_id,
            eval_agent_spec_type,
            build_eval_agent_metadata,
            build_masher_workflows,
        ) = load_masher_components()
        eval_agent_spec = eval_agent_spec_type()
        eval_agent_spec.runtime_context.bind_pool(pool)
        pool.register_agent(
            eval_agent_spec,
            metadata=build_eval_agent_metadata(),
            agent_id=eval_agent_id,
        )
        for workflow in build_masher_workflows(eval_agent_spec):
            pool.register_default_workflow(workflow)
        for workflow in self._workflows:
            pool.register_workflow(workflow)
        # Hosts are defined last so workflow-id validation sees every
        # registered workflow. Pool defaults are appended to both these hosts
        # and hosts defined later through the control API.
        for host in self._hosts:
            pool.define_host(host)
        return pool

"""Builder for pool composition."""

from __future__ import annotations

import importlib
from dataclasses import replace

from mash.core.database import resolve_database_url
from mash.workflows import WorkflowSpec

from ..spec import AgentSpec
from .host import AgentPool
from .subagents import AgentMetadata
from .types import AgentRegistration, Host


def load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.MASHER_AGENT_ID,
        masher.build_masher_workflow_specs,
        masher.create_masher_agent_spec,
    )


class HostBuilder:
    """Builder composing a flat agent pool, workflows, and host definitions."""

    def __init__(self) -> None:
        self._agents: list[AgentRegistration] = []
        self._hosts: list[Host] = []
        self._masher_enabled = True
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

    def enable_masher(self, enabled: bool = True) -> "HostBuilder":
        self._masher_enabled = bool(enabled)
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
        masher_workflow_ids: list[str] = []
        if self._masher_enabled:
            (
                masher_agent_id,
                build_masher_workflow_specs,
                create_masher_agent_spec,
            ) = load_masher_components()
            masher_spec = create_masher_agent_spec()
            # Masher is built at pool startup and needs an LLM provider; skip it
            # when none is configured so keyless deployments still start cleanly.
            if masher_spec.provider_available():
                pool.register_workflow_agent(masher_spec, agent_id=masher_agent_id)
                for workflow in build_masher_workflow_specs(masher_spec):
                    pool.register_workflow(workflow)
                    masher_workflow_ids.append(workflow.workflow_id)
        for workflow in self._workflows:
            pool.register_workflow(workflow)
        # Hosts are defined last so workflow-id validation sees every
        # registered workflow. Masher workflows attach to every built host —
        # they run pool-wide, and attaching keeps them visible in host
        # compositions — appended after any explicitly attached workflows.
        # Conditional on masher actually registering, so keyless deployments
        # still define hosts cleanly.
        for host in self._hosts:
            merged = dict.fromkeys((*host.workflows, *masher_workflow_ids))
            if len(merged) > len(host.workflows):
                host = replace(host, workflows=tuple(merged))
            pool.define_host(host)
        return pool

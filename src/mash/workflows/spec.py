"""Workflow specification types."""

from __future__ import annotations

from dataclasses import dataclass

from mash.runtime.spec import AgentSpec


@dataclass(frozen=True)
class TaskSpec:
    """One workflow task bound to an agent spec."""

    task_id: str
    agent_spec: AgentSpec

    @property
    def agent_id(self) -> str:
        """Resolved agent id for runtime dispatch."""
        return self.agent_spec.get_agent_id()


@dataclass(frozen=True)
class WorkflowSpec:
    """One code-defined workflow composed of ordered tasks."""

    workflow_id: str
    tasks: list[TaskSpec]

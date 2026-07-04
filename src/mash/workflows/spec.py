"""Workflow specification types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mash.runtime.spec import AgentSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .strategy import WorkflowStrategy


@dataclass(frozen=True, init=False)
class TaskSpec:
    """One workflow task bound to an agent spec or registered agent id."""

    task_id: str
    agent_spec: AgentSpec | None
    agent_id: str
    structured_output: dict[str, Any] | None

    def __init__(
        self,
        task_id: str,
        agent_spec: AgentSpec | None = None,
        *,
        agent_id: str | None = None,
        structured_output: dict[str, Any] | None = None,
    ) -> None:
        resolved_task_id = str(task_id or "").strip()
        if not resolved_task_id:
            raise ValueError("task_id is required")
        if agent_spec is None and agent_id is None:
            raise ValueError("agent_spec or agent_id is required")

        resolved_agent_id = str(agent_id or "").strip()
        if agent_spec is not None:
            spec_agent_id = str(agent_spec.get_agent_id() or "").strip()
            if not spec_agent_id:
                raise ValueError("workflow task agent id is required")
            if resolved_agent_id and resolved_agent_id != spec_agent_id:
                raise ValueError(
                    "agent_id must match agent_spec.get_agent_id() when both are provided"
                )
            resolved_agent_id = spec_agent_id
        if not resolved_agent_id:
            raise ValueError("workflow task agent id is required")

        object.__setattr__(self, "task_id", resolved_task_id)
        object.__setattr__(self, "agent_spec", agent_spec)
        object.__setattr__(self, "agent_id", resolved_agent_id)
        object.__setattr__(
            self,
            "structured_output",
            dict(structured_output) if structured_output is not None else None,
        )


@dataclass(frozen=True)
class WorkflowTaskMessageSpec:
    """Dynamic workflow task prompt instructions."""

    skill_name: str

    def __post_init__(self) -> None:
        if not str(self.skill_name or "").strip():
            raise ValueError("workflow task message skill_name is required")


@dataclass(frozen=True)
class WorkflowSpec:
    """One workflow composed of ordered tasks.

    ``strategy`` selects how the workflow executes. When ``None`` (the default)
    the engine runs the ordered ``tasks`` sequentially. A workflow that needs a
    different execution shape (e.g. durable fan-out over its inputs) supplies a
    ``WorkflowStrategy`` that owns both its DBOS registration and its run body,
    keeping the generic engine agnostic to any one workflow.
    """

    workflow_id: str
    tasks: list[TaskSpec]
    metadata: dict[str, Any] = field(default_factory=dict)
    task_message: WorkflowTaskMessageSpec | None = None
    strategy: "WorkflowStrategy | None" = None

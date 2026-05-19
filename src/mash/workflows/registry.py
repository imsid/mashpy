"""Workflow registration helpers."""

from __future__ import annotations

from .spec import WorkflowSpec


class WorkflowRegistry:
    """Registry for code-defined workflows."""

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowSpec] = {}

    def register(self, workflow: WorkflowSpec) -> None:
        workflow_id = str(workflow.workflow_id or "").strip()
        if not workflow_id:
            raise ValueError("workflow_id is required")
        if workflow_id in self._workflows:
            raise ValueError(f"workflow '{workflow_id}' is already registered")
        self._workflows[workflow_id] = workflow

    def get(self, workflow_id: str) -> WorkflowSpec:
        resolved = str(workflow_id or "").strip()
        if not resolved:
            raise ValueError("workflow_id is required")
        try:
            return self._workflows[resolved]
        except KeyError as exc:
            raise KeyError(f"workflow '{resolved}' is not registered") from exc

    def list(self) -> list[WorkflowSpec]:
        return list(self._workflows.values())

"""Workflow registration helpers."""

from __future__ import annotations

from .spec import WorkflowSpec


class WorkflowRegistry:
    """Registry for workflows."""

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowSpec] = {}

    def register(self, workflow: WorkflowSpec) -> None:
        workflow_id = _validate_workflow(workflow)
        if workflow_id in self._workflows:
            raise ValueError(f"workflow '{workflow_id}' is already registered")
        self._workflows[workflow_id] = workflow

    def upsert(self, workflow: WorkflowSpec) -> None:
        workflow_id = _validate_workflow(workflow)
        self._workflows[workflow_id] = workflow

    def unregister(self, workflow_id: str) -> None:
        resolved = str(workflow_id or "").strip()
        if not resolved:
            raise ValueError("workflow_id is required")
        self._workflows.pop(resolved, None)

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


def _validate_workflow(workflow: WorkflowSpec) -> str:
    workflow_id = str(workflow.workflow_id or "").strip()
    if not workflow_id:
        raise ValueError("workflow_id is required")
    # Step pipelines validate themselves in WorkflowSpec.__post_init__; a
    # strategy owns its own execution. One of the two must be present.
    if not workflow.steps and workflow.strategy is None:
        raise ValueError("workflow requires steps or a strategy")
    return workflow_id

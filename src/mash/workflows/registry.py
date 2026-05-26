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
    if not workflow.tasks:
        raise ValueError("workflow tasks are required")

    seen_tasks: set[str] = set()
    for task in workflow.tasks:
        task_id = str(task.task_id or "").strip()
        if not task_id:
            raise ValueError("workflow task_id is required")
        if task_id in seen_tasks:
            raise ValueError(f"workflow task '{task_id}' is already registered")
        seen_tasks.add(task_id)
        if not str(task.agent_id or "").strip():
            raise ValueError("workflow task agent id is required")

    task_message = workflow.task_message
    if task_message is not None:
        if not str(task_message.skill_name or "").strip():
            raise ValueError("workflow task message skill_name is required")
        if not str(task_message.instruction or "").strip():
            raise ValueError("workflow task message instruction is required")
    return workflow_id

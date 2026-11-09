"""The workflow list the companion gets in a host request's context."""

from __future__ import annotations

import json
from typing import Any, Iterable

from mash.workflows import WorkflowSpec


def _strip_titles(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_strip_titles(v) for v in value]
    return value


def _input_schema(workflow: WorkflowSpec) -> str:
    if workflow.input_model is None:
        return "{}"
    schema = _strip_titles(workflow.input_model.model_json_schema())
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def format_workflow_list(workflows: Iterable[WorkflowSpec]) -> str:
    """The ``WORKFLOWS`` block: id, display name, description and input schema."""
    lines = ["WORKFLOWS", "Run one with RunWorkflow(workflow_id, input)."]
    count = 0
    for workflow in workflows:
        metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
        name = str(metadata.get("display_name") or workflow.workflow_id).strip()
        description = str(metadata.get("description") or "").strip()
        head = f"- {workflow.workflow_id} | {name}"
        lines.append(f"{head}: {description}" if description else head)
        lines.append(f"  Input: {_input_schema(workflow)}")
        count += 1
    return "\n".join(lines) if count else ""


__all__ = ["format_workflow_list"]

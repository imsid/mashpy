"""Workflow orchestration exports.

Lazy exports: the authoring types in ``spec.py`` must be importable without
loading the DBOS-backed service modules this package also exports.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import WorkflowRegistry
    from .service import (
        DuplicateWorkflowRunError,
        WorkflowInputValidationError,
        WorkflowNotFoundError,
        WorkflowRun,
        WorkflowService,
        WorkflowStreamEvent,
    )
    from .spec import (
        AgentStep,
        CodeStep,
        StepContext,
        StepSpec,
        WorkflowSpec,
        validate_step_pipeline,
    )

__all__ = [
    "AgentStep",
    "CodeStep",
    "DuplicateWorkflowRunError",
    "StepContext",
    "StepSpec",
    "WorkflowInputValidationError",
    "WorkflowNotFoundError",
    "WorkflowRegistry",
    "WorkflowRun",
    "WorkflowService",
    "WorkflowStreamEvent",
    "WorkflowSpec",
    "validate_step_pipeline",
]

_EXPORTS: dict[str, str] = {
    "AgentStep": ".spec",
    "CodeStep": ".spec",
    "StepContext": ".spec",
    "StepSpec": ".spec",
    "WorkflowSpec": ".spec",
    "validate_step_pipeline": ".spec",
    "WorkflowRegistry": ".registry",
    "DuplicateWorkflowRunError": ".service",
    "WorkflowInputValidationError": ".service",
    "WorkflowNotFoundError": ".service",
    "WorkflowRun": ".service",
    "WorkflowService": ".service",
    "WorkflowStreamEvent": ".service",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value

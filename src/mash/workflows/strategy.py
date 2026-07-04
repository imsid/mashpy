"""Workflow execution strategies.

A strategy owns everything workflow-specific about how a workflow runs: its
optional DBOS registration (queues, child workflows) and its run body. The
generic engine dispatches to the strategy the ``WorkflowSpec`` carries, so it
never needs to special-case any particular workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spec import WorkflowSpec


@dataclass(frozen=True)
class WorkflowExecutionContext:
    """Inputs a strategy needs to execute one workflow run."""

    runner_id: str
    workflow: WorkflowSpec
    run_id: str
    workflow_input: dict[str, Any]
    session_id: str | None = None


class WorkflowStrategy:
    """Base class for workflow execution strategies.

    Subclasses must implement :meth:`run`. :meth:`register` is optional and is
    invoked once, before ``DBOS.launch()``, so a strategy can register any DBOS
    queues or child workflows it depends on. It must be idempotent.
    """

    def register(self, dbos_class: Any) -> None:  # noqa: D401 - hook
        """Register DBOS objects this strategy needs. Idempotent no-op by default."""

    async def run(self, ctx: WorkflowExecutionContext) -> dict[str, Any]:
        raise NotImplementedError

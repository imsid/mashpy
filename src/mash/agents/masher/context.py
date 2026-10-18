"""Runtime dependencies shared by Masher's workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...runtime.events import RuntimeStore

if TYPE_CHECKING:
    from ...evals.service import EvalService


@dataclass
class MasherRuntimeContext:
    """Runtime dependencies for Masher's workflow steps.

    Workflow code steps close over this context at build time; the actual
    dependencies are bound later — the pool at ``HostBuilder.build()``, the eval
    service at API startup — so ``require_*`` accessors resolve lazily.
    """

    eval_service: "EvalService | None" = None
    pool: Any = None

    def bind_eval_service(self, eval_service: "EvalService") -> None:
        self.eval_service = eval_service

    def bind_pool(self, pool: Any) -> None:
        self.pool = pool

    def require_runtime_store(self) -> RuntimeStore:
        # The pool's shared store exists only after pool.start(), which always
        # precedes any workflow run.
        store = self.require_pool().get_runtime_store()
        if store is None:
            raise RuntimeError("Masher runtime store is not bound")
        return store

    def require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("Masher workflow agent pool is not bound")
        return self.pool

    def require_eval_service(self) -> "EvalService":
        if self.eval_service is None:
            raise RuntimeError("Masher eval service is not bound")
        return self.eval_service


__all__ = ["MasherRuntimeContext"]

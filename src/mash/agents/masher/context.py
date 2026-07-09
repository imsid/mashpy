"""Runtime dependencies shared by Masher's workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...runtime.events import RuntimeStore

if TYPE_CHECKING:
    from ...evals.service import EvalService


@dataclass
class MasherRuntimeContext:
    """Runtime dependencies and artifact paths for Masher's workflow steps.

    Workflow code steps close over this context at build time; the actual
    dependencies are bound later — the pool at ``HostBuilder.build()``, the eval
    service at API startup — so ``require_*`` accessors resolve lazily.
    """

    runtime_store: RuntimeStore | None = None
    trace_digest_jsonl_path: Path | None = None
    online_eval_jsonl_path: Path | None = None
    eval_service: "EvalService | None" = None
    pool: Any = None

    def bind_runtime_store(self, runtime_store: RuntimeStore) -> None:
        self.runtime_store = runtime_store

    def bind_eval_service(self, eval_service: "EvalService") -> None:
        self.eval_service = eval_service

    def bind_pool(self, pool: Any) -> None:
        self.pool = pool

    def configure_artifacts(self, data_root: Path) -> None:
        masher_root = data_root / "masher"
        self.trace_digest_jsonl_path = (masher_root / "trace-digests.jsonl").resolve()
        self.online_eval_jsonl_path = (masher_root / "online-evals.jsonl").resolve()

    def require_runtime_store(self) -> RuntimeStore:
        # The pool's shared store exists only after pool.start(), which always
        # precedes any workflow run — hence the lazy fallback instead of an
        # eager bind. Keyless deployments have no Masher agent, so this is the
        # only store path for the all-code workflows.
        if self.runtime_store is None and self.pool is not None:
            self.runtime_store = self.pool.get_runtime_store()
        if self.runtime_store is None:
            raise RuntimeError("Masher runtime store is not bound")
        return self.runtime_store

    def require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("Masher agent pool is not bound")
        return self.pool

    def require_eval_service(self) -> "EvalService":
        if self.eval_service is None:
            raise RuntimeError("Masher eval service is not bound")
        return self.eval_service

    def require_trace_digest_jsonl_path(self) -> Path:
        if self.trace_digest_jsonl_path is None:
            raise RuntimeError("Masher trace digest artifact path is not configured")
        return self.trace_digest_jsonl_path

    def require_online_eval_jsonl_path(self) -> Path:
        if self.online_eval_jsonl_path is None:
            raise RuntimeError("Masher online eval artifact path is not configured")
        return self.online_eval_jsonl_path


__all__ = ["MasherRuntimeContext"]

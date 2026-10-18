"""Runtime dependencies shared by the cortex-build workflow's steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mash.core.database import resolve_database_url

from .models import CortexSettings
from .store import CortexStore


@dataclass
class CortexRuntimeContext:
    """The pool, settings and store the cortex-build steps close over.

    The pool is bound at ``HostBuilder.build()``; the store opens on first use,
    on the host, where workflow code steps run.
    """

    settings: CortexSettings
    pool: Any = None
    store: CortexStore | None = None

    def bind_pool(self, pool: Any) -> None:
        self.pool = pool

    def require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("Cortex workflow pool is not bound")
        return self.pool

    def require_store(self) -> CortexStore:
        if self.store is None:
            database_url = self.require_pool().runtime_database_url or resolve_database_url()
            if not database_url:
                raise RuntimeError("MASH_DATABASE_URL is required for Cortex")
            self.store = CortexStore(database_url)
        return self.store


__all__ = ["CortexRuntimeContext"]

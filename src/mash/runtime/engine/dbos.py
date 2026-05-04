"""DBOS-backed request engine."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .protocol import RequestEngine
from .workflow import execute_request_workflow, workflow_id_for

if TYPE_CHECKING:
    from ..service import AgentRuntime


@dataclass
class _DBOSRuntimeState:
    ready: bool = False
    database_url: str | None = None
    registered_workflow: Any = None
    runtime_registry: dict[str, "AgentRuntime"] = field(default_factory=dict)


_STATE = _DBOSRuntimeState()


def _load_dbos_api() -> tuple[Any, Any]:
    try:
        module = importlib.import_module("dbos")
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise RuntimeError(
            "dbos is required for hosted runtime execution. Install mashpy with DBOS dependencies."
        ) from exc

    dbos_class = getattr(module, "DBOS", None)
    set_workflow_id = getattr(module, "SetWorkflowID", None)
    if dbos_class is None or set_workflow_id is None:
        raise RuntimeError("dbos module is missing required runtime APIs")
    return dbos_class, set_workflow_id


def require_runtime(agent_id: str) -> "AgentRuntime":
    runtime = _STATE.runtime_registry.get(agent_id)
    if runtime is None:
        raise RuntimeError(f"runtime '{agent_id}' is not registered")
    return runtime


def register_runtime(runtime: "AgentRuntime") -> None:
    _STATE.runtime_registry[runtime.app_id] = runtime


def unregister_runtime(runtime: "AgentRuntime") -> None:
    existing = _STATE.runtime_registry.get(runtime.app_id)
    if existing is runtime:
        _STATE.runtime_registry.pop(runtime.app_id, None)


async def ensure_dbos_ready(database_url: str) -> None:
    resolved_url = str(database_url or "").strip()
    conductor_key = os.getenv("DBOS_CONDUCTOR_KEY")
    if not resolved_url:
        raise RuntimeError("MASH_RUNTIME_DATABASE_URL is required")
    if not conductor_key:
        raise RuntimeError("DBOS_CONDUCTOR_KEY is required")
    if _STATE.ready:
        if _STATE.database_url != resolved_url:
            raise RuntimeError(
                "DBOS runtime is already initialized with a different database URL"
            )
        return
    dbos_class, _ = _load_dbos_api()

    dbos_class(
        config={
            "name": "mash",
            "system_database_url": resolved_url,
            "conductor_key": conductor_key,
        }
    )
    register_workflow(dbos_class)
    dbos_class.launch()
    _STATE.ready = True
    _STATE.database_url = resolved_url


def register_workflow(dbos_class: Any) -> None:
    if _STATE.registered_workflow is not None:
        return

    async def _workflow(
        agent_id: str,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
    ) -> None:
        await execute_request_workflow(
            agent_id,
            request_id,
            message,
            session_id,
            request_metadata,
            require_runtime=require_runtime,
        )

    _STATE.registered_workflow = dbos_class.workflow(
        name="mash.runtime.execute_request"
    )(_workflow)


class DBOSRequestEngine(RequestEngine):
    def __init__(self, runtime: "AgentRuntime", *, database_url: str) -> None:
        self._runtime = runtime
        self._database_url = str(database_url or "").strip()

    async def open(self) -> None:
        register_runtime(self._runtime)
        await ensure_dbos_ready(self._database_url)

    async def close(self) -> None:
        unregister_runtime(self._runtime)

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
    ) -> None:
        await ensure_dbos_ready(self._database_url)
        dbos_class, set_workflow_id = _load_dbos_api()

        workflow = _STATE.registered_workflow
        if workflow is None:
            raise RuntimeError("DBOS workflow is not registered")
        with set_workflow_id(workflow_id_for(self._runtime.app_id, request_id)):
            await dbos_class.start_workflow_async(
                workflow,
                self._runtime.app_id,
                request_id,
                message,
                session_id,
                dict(request_metadata or {}),
            )


__all__ = [
    "DBOSRequestEngine",
    "RequestEngine",
    "ensure_dbos_ready",
]

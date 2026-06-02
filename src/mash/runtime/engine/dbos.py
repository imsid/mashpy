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
    conductor_key = os.getenv("DBOS_CONDUCTOR_KEY") or None
    if not resolved_url:
        raise RuntimeError("MASH_DATABASE_URL is required")
    if _STATE.ready:
        if _STATE.database_url != resolved_url:
            raise RuntimeError(
                "DBOS runtime is already initialized with a different database URL"
            )
        return
    dbos_class, _ = _load_dbos_api()

    config: dict[str, Any] = {
        "name": "mash",
        "system_database_url": resolved_url,
    }
    if conductor_key:
        config["conductor_key"] = conductor_key
    dbos_class(config=config)
    register_workflow(dbos_class)
    dbos_class.launch()
    _STATE.ready = True
    _STATE.database_url = resolved_url


def register_workflow(dbos_class: Any) -> None:
    from mash.workflows.dbos import register_workflow as register_host_workflow

    if _STATE.registered_workflow is not None:
        register_host_workflow(dbos_class)
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
    register_host_workflow(dbos_class)


_REQUEST_STATUS_MAP = {
    "PENDING": "pending",
    "SUCCESS": "completed",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
    "ENQUEUED": "queued",
    "DELAYED": "queued",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "failed",
}


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
        workflow_id = workflow_id_for(self._runtime.app_id, request_id)
        try:
            with set_workflow_id(workflow_id):
                await dbos_class.start_workflow_async(
                    workflow,
                    self._runtime.app_id,
                    request_id,
                    message,
                    session_id,
                    dict(request_metadata or {}),
                )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(
                f"failed to start DBOS workflow '{workflow_id}' for session "
                f"'{session_id}': {detail}"
            ) from exc

    async def get_request_status(
        self,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        await ensure_dbos_ready(self._database_url)
        dbos_class, _ = _load_dbos_api()
        workflow_id = workflow_id_for(self._runtime.app_id, request_id)
        status = await dbos_class.get_workflow_status_async(workflow_id)
        if status is None:
            raise KeyError(f"request '{request_id}' not found")
        raw_status = str(getattr(status, "status", "") or "")
        result: dict[str, Any] = {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": _REQUEST_STATUS_MAP.get(raw_status, "unknown"),
            "dbos_status": raw_status,
        }
        error = getattr(status, "error", None)
        if error is not None:
            result["error"] = str(error)
        recovery_attempts = getattr(status, "recovery_attempts", None)
        if recovery_attempts is not None:
            result["recovery_attempts"] = int(recovery_attempts)
        return result

    async def resume_request(
        self,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        await ensure_dbos_ready(self._database_url)
        dbos_class, _ = _load_dbos_api()
        workflow_id = workflow_id_for(self._runtime.app_id, request_id)
        status = await dbos_class.get_workflow_status_async(workflow_id)
        if status is None:
            raise KeyError(f"request '{request_id}' not found")
        raw_status = str(getattr(status, "status", "") or "")
        if raw_status == "SUCCESS":
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "status": "completed",
                "message": "request already completed successfully",
            }
        if raw_status == "PENDING":
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "status": "pending",
                "message": "request is already pending recovery",
            }
        if raw_status in ("ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
            await dbos_class.resume_workflow_async(workflow_id)
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "status": "resumed",
                "previous_status": _REQUEST_STATUS_MAP.get(raw_status, "unknown"),
                "message": "request has been resumed for recovery",
            }
        return {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": _REQUEST_STATUS_MAP.get(raw_status, "unknown"),
            "message": f"request is in '{raw_status}' state",
        }


__all__ = [
    "DBOSRequestEngine",
    "RequestEngine",
    "ensure_dbos_ready",
]

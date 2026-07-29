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


async def start_request_workflow(
    agent_id: str,
    request_id: str,
    message: str,
    session_id: str,
    request_metadata: dict[str, Any],
    *,
    require_runtime_fallback: Any | None = None,
) -> None:
    """Start execute_request_workflow as a standalone DBOS workflow.

    Used by the workflow orchestrator for inline task execution so that the
    child workflow gets its own DBOS workflow ID (``agent_id:request_id``).
    This is required for ``DBOS.recv_async`` / ``DBOS.send_async`` pairing
    used by AskUser interactions.

    When the DBOS runtime workflow is not registered (e.g. in tests),
    falls back to executing the request workflow inline if
    *require_runtime_fallback* is provided.
    """
    workflow = _STATE.registered_workflow
    if workflow is not None:
        dbos_class, set_workflow_id = _load_dbos_api()
        wf_id = workflow_id_for(agent_id, request_id)
        with set_workflow_id(wf_id):
            await dbos_class.start_workflow_async(
                workflow,
                agent_id,
                request_id,
                message,
                session_id,
                dict(request_metadata or {}),
            )
        return
    if require_runtime_fallback is not None:
        await execute_request_workflow(
            agent_id,
            request_id,
            message,
            session_id,
            dict(request_metadata or {}),
            require_runtime=require_runtime_fallback,
        )
        return
    raise RuntimeError("DBOS workflow is not registered")


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
        if raw_status == "ERROR":
            # DBOS refuses to resume a workflow that ended in ERROR: its
            # ``resume_workflows`` update excludes SUCCESS and ERROR, so the
            # call is a silent no-op. Resuming a failed request from its last
            # checkpoint needs ``fork_workflow``, which mints a new workflow
            # id and so breaks the request/workflow/trace 1:1 mapping — a
            # separate change. Until then, say so instead of reporting a
            # resume that never happened.
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "status": "failed",
                "message": (
                    "failed requests cannot be resumed; rerun the request "
                    "to start it over"
                ),
            }
        if raw_status in ("CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
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

    async def cancel_request(
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
        if raw_status in (
            "SUCCESS",
            "ERROR",
            "CANCELLED",
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
        ):
            return {
                "request_id": request_id,
                "workflow_id": workflow_id,
                "status": _REQUEST_STATUS_MAP.get(raw_status, "unknown"),
                "message": "request is already in a terminal state",
            }
        # Preempt the workflow at the next step boundary. The in-flight step
        # finishes and checkpoints; DBOSWorkflowCancelledError bypasses the
        # workflow's error handler, so this path owns the terminal event.
        await dbos_class.cancel_workflow_async(workflow_id)
        await self._settle_cancelled_interaction(
            request_id, workflow_id, dbos_class
        )
        return {
            "request_id": request_id,
            "workflow_id": workflow_id,
            "status": "cancelled",
            "message": "request has been cancelled",
        }

    async def _settle_cancelled_interaction(
        self,
        request_id: str,
        workflow_id: str,
        dbos_class: Any,
    ) -> None:
        """Clear a parked interaction and emit the terminal cancelled event.

        Reads the request's events once: if the request is blocked on an
        interaction, acks it cancelled (so the UI clears the prompt) and sends
        the cancel sentinel to its topic (so a later resume replays into a
        fresh attempt). Then appends ``request.cancelled``.
        """
        from ..requests import find_pending_interaction
        from .steps import (
            INTERACTION_CANCEL_SENTINEL,
            emit_interaction_ack,
            emit_request_cancelled,
        )

        events = await self._runtime.runtime_store.list_request_events(request_id)
        trace_id = next((e.trace_id for e in events if e.trace_id), None)
        session_id = next((e.session_id for e in events if e.session_id), None)

        pending_interaction_id = find_pending_interaction(events)
        if pending_interaction_id is not None:
            await emit_interaction_ack(
                self._runtime.app_id,
                request_id,
                session_id,
                trace_id,
                interaction_id=pending_interaction_id,
                response=None,
                cancelled=True,
            )
            try:
                await dbos_class.send_async(
                    workflow_id,
                    INTERACTION_CANCEL_SENTINEL,
                    topic=pending_interaction_id,
                )
            except Exception:
                # A cancelled workflow may reject the send; the ack already
                # cleared the prompt and resume issues a fresh interaction
                # regardless, so this is non-fatal.
                pass

        await emit_request_cancelled(
            self._runtime.app_id,
            request_id,
            session_id,
            trace_id,
        )


__all__ = [
    "DBOSRequestEngine",
    "RequestEngine",
    "ensure_dbos_ready",
]

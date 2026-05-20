"""Workflow orchestration service."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import to_public_event

from . import dbos as workflow_dbos
from .registry import WorkflowRegistry
from .spec import WorkflowSpec

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentHost


@dataclass
class WorkflowRun:
    """One workflow invocation projected from DBOS workflow state."""

    run_id: str
    workflow_id: str
    dedup_key: str | None
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    output: dict[str, Any] | None = None


@dataclass
class WorkflowStreamEvent:
    """One public workflow SSE event."""

    event: str
    data: dict[str, Any]
    comment: str | None = None


class WorkflowNotFoundError(LookupError):
    """Raised when a requested workflow is not registered."""


class DuplicateWorkflowRunError(RuntimeError):
    """Raised when the same workflow dedup key is already active."""

    def __init__(self, workflow_id: str, dedup_key: str, existing_run: WorkflowRun) -> None:
        self.workflow_id = workflow_id
        self.dedup_key = dedup_key
        self.existing_run = existing_run
        super().__init__(
            f"workflow '{workflow_id}' already has an active run for dedup key '{dedup_key}'"
        )


class WorkflowService:
    """Host-level workflow execution service."""

    def __init__(
        self,
        workflow_registry: WorkflowRegistry,
        host: "AgentHost",
        *,
        host_id: str,
    ) -> None:
        self._workflow_registry = workflow_registry
        self._host = host
        self._host_id = str(host_id or "").strip()
        if not self._host_id:
            raise ValueError("host_id is required")

    async def list_workflows(self) -> list[dict[str, Any]]:
        return [self._serialize_workflow(item) for item in self._workflow_registry.list()]

    async def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        normalized_dedup_key = _normalize_optional_text(dedup_key)
        normalized_workflow_input = _normalize_workflow_input(workflow_input)
        workflow = self._require_workflow(resolved_workflow_id)
        database_url = str(getattr(self._host, "runtime_database_url", "") or "").strip()
        if not database_url:
            raise RuntimeError("MASH_RUNTIME_DATABASE_URL is required")

        try:
            run_id = await workflow_dbos.start_workflow_run(
                database_url=database_url,
                host_id=self._host_id,
                workflow=workflow,
                dedup_key=normalized_dedup_key,
                workflow_input=normalized_workflow_input,
            )
        except workflow_dbos.WorkflowDeduplicatedError as exc:
            existing_run = WorkflowRun(
                run_id=exc.run_id,
                workflow_id=resolved_workflow_id,
                dedup_key=normalized_dedup_key,
                status="running",
                created_at=time.time(),
            )
            raise DuplicateWorkflowRunError(
                resolved_workflow_id,
                normalized_dedup_key or "",
                existing_run,
            ) from exc

        status = await workflow_dbos.get_workflow_status(run_id)
        if status is not None:
            return _run_from_status(
                status,
                workflow_id=resolved_workflow_id,
                dedup_key=normalized_dedup_key,
            )
        return WorkflowRun(
            run_id=run_id,
            workflow_id=resolved_workflow_id,
            dedup_key=normalized_dedup_key,
            status="queued",
            created_at=time.time(),
        )

    async def get_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        self._require_workflow(resolved_workflow_id)
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")
        expected_prefix = workflow_dbos.workflow_run_id_prefix(
            self._host_id,
            resolved_workflow_id,
        )
        if not resolved_run_id.startswith(expected_prefix):
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' is not registered for workflow '{resolved_workflow_id}'"
            )

        status = await workflow_dbos.get_workflow_status(resolved_run_id)
        if status is None:
            raise WorkflowNotFoundError(f"workflow run '{resolved_run_id}' was not found")
        return _run_from_status(
            status,
            workflow_id=resolved_workflow_id,
            dedup_key=_dedup_key_from_status(status, resolved_workflow_id),
        )

    async def stream_run_events(
        self,
        workflow_id: str,
        run_id: str,
        *,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[WorkflowStreamEvent]:
        resolved_workflow_id = str(workflow_id or "").strip()
        resolved_run_id = str(run_id or "").strip()
        workflow = self._require_workflow(resolved_workflow_id)
        initial_run = await self.get_run(resolved_workflow_id, resolved_run_id)

        async def _generate() -> AsyncIterator[WorkflowStreamEvent]:
            cursors: dict[str, int] = {task.task_id: 0 for task in workflow.tasks}
            started_tasks: set[str] = set()
            completed_tasks: set[str] = set()
            error_tasks: set[str] = set()
            last_status: str | None = None
            run = initial_run

            while True:
                if run.status != last_status:
                    yield _workflow_status_event(run)
                    last_status = run.status

                emitted = False
                for task in workflow.tasks:
                    task_events = await self._list_task_runtime_events(
                        task.agent_id,
                        workflow_id=resolved_workflow_id,
                        task_id=task.task_id,
                        run_id=resolved_run_id,
                        after_event_id=cursors.get(task.task_id, 0),
                    )
                    if not task_events:
                        continue

                    if task.task_id not in started_tasks:
                        started_tasks.add(task.task_id)
                        emitted = True
                        yield _workflow_task_event(
                            "workflow.task.started",
                            workflow_id=resolved_workflow_id,
                            run_id=resolved_run_id,
                            task_id=task.task_id,
                            task_agent_id=task.agent_id,
                        )

                    for event in task_events:
                        cursors[task.task_id] = max(
                            cursors.get(task.task_id, 0),
                            int(getattr(event, "event_id", 0) or 0),
                        )
                        emitted = True
                        public = to_public_event(event)
                        yield WorkflowStreamEvent(
                            event=str(public.get("event") or "message"),
                            data=_augment_workflow_payload(
                                public.get("data"),
                                workflow_id=resolved_workflow_id,
                                run_id=resolved_run_id,
                                task_id=task.task_id,
                                task_agent_id=task.agent_id,
                            ),
                        )

                        if event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
                            if task.task_id not in completed_tasks:
                                completed_tasks.add(task.task_id)
                                yield _workflow_task_event(
                                    "workflow.task.completed",
                                    workflow_id=resolved_workflow_id,
                                    run_id=resolved_run_id,
                                    task_id=task.task_id,
                                    task_agent_id=task.agent_id,
                                )
                        elif event.event_type == RuntimeEventType.REQUEST_FAILED.value:
                            if task.task_id not in error_tasks:
                                error_tasks.add(task.task_id)
                                yield _workflow_task_event(
                                    "workflow.task.error",
                                    workflow_id=resolved_workflow_id,
                                    run_id=resolved_run_id,
                                    task_id=task.task_id,
                                    task_agent_id=task.agent_id,
                                )

                if _is_terminal_workflow_status(run.status):
                    return

                if not emitted:
                    yield WorkflowStreamEvent(event="", data={}, comment="keep-alive")
                    await _sleep(poll_interval)
                run = await self.get_run(resolved_workflow_id, resolved_run_id)

        return _generate()

    async def _list_task_runtime_events(
        self,
        agent_id: str,
        *,
        workflow_id: str,
        task_id: str,
        run_id: str,
        after_event_id: int,
    ) -> list[Any]:
        agent = self._host.get_agent(agent_id)
        return await agent.runtime_store.list_events(
            app_id=agent_id,
            session_id=workflow_task_session_id(
                workflow_id=workflow_id,
                task_id=task_id,
                run_id=run_id,
            ),
            after_event_id=max(0, int(after_event_id)),
        )

    def _require_workflow(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflow_registry.get(workflow_id)
        except KeyError as exc:
            raise WorkflowNotFoundError(
                f"workflow '{workflow_id}' is not registered"
            ) from exc

    @staticmethod
    def _serialize_workflow(workflow: WorkflowSpec) -> dict[str, Any]:
        return {
            "workflow_id": workflow.workflow_id,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                }
                for task in workflow.tasks
            ],
        }


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _normalize_workflow_input(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("workflow_input must be a JSON object")
    return dict(value)


def workflow_task_session_id(*, workflow_id: str, task_id: str, run_id: str) -> str:
    return f"workflow:{workflow_id}:task:{task_id}:run:{run_id}"


def _workflow_status_event(run: WorkflowRun) -> WorkflowStreamEvent:
    return WorkflowStreamEvent(
        event="workflow.status",
        data={
            "workflow_id": run.workflow_id,
            "run_id": run.run_id,
            "dedup_key": run.dedup_key,
            "status": run.status,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error": run.error,
            "output": run.output,
        },
    )


def _workflow_task_event(
    event_name: str,
    *,
    workflow_id: str,
    run_id: str,
    task_id: str,
    task_agent_id: str,
) -> WorkflowStreamEvent:
    return WorkflowStreamEvent(
        event=event_name,
        data={
            "workflow_id": workflow_id,
            "run_id": run_id,
            "task_id": task_id,
            "task_agent_id": task_agent_id,
        },
    )


def _augment_workflow_payload(
    payload: Any,
    *,
    workflow_id: str,
    run_id: str,
    task_id: str,
    task_agent_id: str,
) -> dict[str, Any]:
    data = dict(payload) if isinstance(payload, dict) else {"payload": payload}
    data.update(
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "task_id": task_id,
            "task_agent_id": task_agent_id,
        }
    )
    return data


def _is_terminal_workflow_status(status: str) -> bool:
    return str(status or "").lower() in {"completed", "failed", "cancelled"}


async def _sleep(duration: float) -> None:
    await asyncio.sleep(max(0.0, float(duration)))


def _run_from_status(
    status: Any,
    *,
    workflow_id: str,
    dedup_key: str | None,
) -> WorkflowRun:
    raw_status = str(getattr(status, "status", "") or "")
    created_at = _millis_to_seconds(getattr(status, "created_at", None)) or time.time()
    updated_at = _millis_to_seconds(getattr(status, "updated_at", None))
    mapped_status = _map_dbos_status(raw_status)
    return WorkflowRun(
        run_id=str(getattr(status, "workflow_id", "") or ""),
        workflow_id=workflow_id,
        dedup_key=dedup_key,
        status=mapped_status,
        created_at=created_at,
        started_at=created_at if mapped_status not in {"queued"} else None,
        finished_at=updated_at if mapped_status in {"completed", "failed", "cancelled"} else None,
        error=_status_error(status),
        output=_status_output(status),
    )


def _map_dbos_status(status: str) -> str:
    return {
        "PENDING": "queued",
        "ENQUEUED": "queued",
        "DELAYED": "queued",
        "SUCCESS": "completed",
        "ERROR": "failed",
        "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "failed",
        "CANCELLED": "cancelled",
    }.get(status, status.lower() or "unknown")


def _millis_to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _status_error(status: Any) -> str | None:
    error = getattr(status, "error", None)
    if error is None:
        return None
    text = str(error).strip()
    return text or None


def _status_output(status: Any) -> dict[str, Any] | None:
    output = getattr(status, "output", None)
    return dict(output) if isinstance(output, dict) else None


def _dedup_key_from_status(status: Any, workflow_id: str) -> str | None:
    dedup = getattr(status, "deduplication_id", None)
    if not isinstance(dedup, str) or not dedup:
        return None
    prefix = f"{workflow_id}:"
    return dedup[len(prefix) :] if dedup.startswith(prefix) else dedup

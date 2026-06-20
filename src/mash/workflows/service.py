"""Workflow orchestration service."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import to_public_event

from . import dbos as workflow_dbos
from .registry import WorkflowRegistry
from .spec import WorkflowSpec

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentPool


@dataclass
class WorkflowRun:
    """One workflow invocation projected for public workflow APIs."""

    run_id: str
    workflow_id: str
    dedup_key: str | None
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    output: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None


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
        pool: "AgentPool",
        *,
        runner_id: str,
    ) -> None:
        self._workflow_registry = workflow_registry
        self._pool = pool
        self._runner_id = str(runner_id or "").strip()
        if not self._runner_id:
            raise ValueError("runner_id is required")

    async def list_workflows(self) -> list[dict[str, Any]]:
        return [self._serialize_workflow(item) for item in self._workflow_registry.list()]

    async def list_runs(
        self,
        workflow_id: str,
        *,
        status: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> list[WorkflowRun]:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        workflow = self._require_workflow(resolved_workflow_id)
        normalized_status = _normalize_optional_text(status)
        if normalized_status is not None and normalized_status.lower() != "completed":
            return []
        parsed_start_time = _parse_time_filter(start_time)
        parsed_end_time = _parse_time_filter(end_time)

        turns: list[dict[str, Any]] = []
        for agent_id in self._workflow_task_agent_ids(workflow):
            store = self._workflow_task_memory_store(agent_id)
            if store is None:
                continue
            task_turns = await store.list_workflow_turns(
                app_id=agent_id,
                workflow_id=resolved_workflow_id,
                start_time=parsed_start_time,
                end_time=parsed_end_time,
                limit=None,
                offset=0,
                sort_desc=bool(sort_desc),
            )
            for turn in task_turns:
                run_id = str(turn.get("workflow_run_id") or "").strip()
                if not run_id:
                    continue
                item = dict(turn)
                item["workflow_id"] = resolved_workflow_id
                item["task_id"] = turn.get("task_id")
                item["run_id"] = run_id
                item["agent_id"] = agent_id
                turns.append(item)

        return _runs_from_workflow_turns(
            turns,
            workflow_id=resolved_workflow_id,
            limit=max(1, int(limit)),
            offset=max(0, int(offset)),
            sort_desc=bool(sort_desc),
        )

    async def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> WorkflowRun:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        normalized_dedup_key = _normalize_optional_text(dedup_key)
        normalized_workflow_input = _normalize_workflow_input(workflow_input)
        normalized_session_id = _normalize_optional_text(session_id)
        workflow = self._require_workflow(resolved_workflow_id)
        database_url = str(getattr(self._pool, "runtime_database_url", "") or "").strip()
        if not database_url:
            raise RuntimeError("MASH_DATABASE_URL is required")

        try:
            run_id = await workflow_dbos.start_workflow_run(
                database_url=database_url,
                runner_id=self._runner_id,
                workflow=workflow,
                dedup_key=normalized_dedup_key,
                workflow_input=normalized_workflow_input,
                session_id=normalized_session_id,
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

        try:
            status = await workflow_dbos.get_workflow_status(resolved_run_id)
        except Exception as exc:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            ) from exc
        if status is None:
            raise WorkflowNotFoundError(f"workflow run '{resolved_run_id}' was not found")
        run = _run_from_status(
            status,
            workflow_id=resolved_workflow_id,
            dedup_key=_dedup_key_from_status(status, resolved_workflow_id),
        )
        run.summary = await self._workflow_run_summary(resolved_workflow_id, resolved_run_id)
        return run

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
        if not resolved_run_id:
            raise ValueError("run_id is required")

        initial_events: dict[str, list[Any]] = {}
        for task in workflow.tasks:
            task_events = await self._list_task_runtime_events(
                task.agent_id,
                workflow_id=resolved_workflow_id,
                task_id=task.task_id,
                run_id=resolved_run_id,
                after_event_id=0,
            )
            initial_events[task.task_id] = task_events
        if not any(initial_events.values()):
            status = await _get_workflow_status_or_none(resolved_run_id)
            if status is None:
                raise WorkflowNotFoundError(
                    f"workflow run '{resolved_run_id}' was not found"
                )

        async def _generate() -> AsyncIterator[WorkflowStreamEvent]:
            cursors: dict[str, int] = {task.task_id: 0 for task in workflow.tasks}
            started_tasks: set[str] = set()
            completed_tasks: set[str] = set()
            error_tasks: set[str] = set()
            queued_events = initial_events

            while True:
                emitted = False
                for task in workflow.tasks:
                    task_events = queued_events.pop(task.task_id, None)
                    if task_events is None:
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

                if error_tasks:
                    return

                status = None
                if len(completed_tasks) == len(workflow.tasks):
                    status = await workflow_dbos.get_workflow_status(resolved_run_id)
                    terminal_event = _terminal_workflow_stream_event(
                        status,
                        workflow_id=resolved_workflow_id,
                        run_id=resolved_run_id,
                    )
                    if terminal_event is not None:
                        if terminal_event.event:
                            yield terminal_event
                        return
                elif not emitted:
                    status = await workflow_dbos.get_workflow_status(resolved_run_id)
                    terminal_event = _terminal_workflow_stream_event(
                        status,
                        workflow_id=resolved_workflow_id,
                        run_id=resolved_run_id,
                    )
                    if terminal_event is not None:
                        if terminal_event.event:
                            yield terminal_event
                        return
                if not emitted:
                    yield WorkflowStreamEvent(event="", data={}, comment="keep-alive")
                    await _sleep(poll_interval)

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
        agent = self._pool.get_agent(agent_id)
        return await agent.runtime_store.list_events(
            app_id=agent_id,
            workflow_run_id=run_id,
            after_event_id=max(0, int(after_event_id)),
        )

    async def _workflow_run_summary(
        self,
        workflow_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        workflow = self._require_workflow(workflow_id)
        turns: list[dict[str, Any]] = []
        for agent_id in self._workflow_task_agent_ids(workflow):
            store = self._workflow_task_memory_store(agent_id)
            if store is None:
                continue
            task_turns = await store.list_workflow_turns(
                app_id=agent_id,
                workflow_id=workflow_id,
                workflow_run_id=run_id,
                limit=None,
                offset=0,
                sort_desc=True,
            )
            for turn in task_turns:
                item = dict(turn)
                item["task_id"] = turn.get("task_id")
                item["agent_id"] = agent_id
                turns.append(item)
        if not turns:
            return None
        latest = max(
            turns,
            key=lambda item: (
                float(item.get("created_at") or 0.0),
                str(item.get("turn_id") or ""),
            ),
        )
        return _workflow_run_summary_from_turn(latest)

    @staticmethod
    def _workflow_task_agent_ids(workflow: Any) -> list[str]:
        """Distinct task agent ids, preserving order (turns carry task_id)."""
        seen: list[str] = []
        for task in workflow.tasks:
            if task.agent_id not in seen:
                seen.append(task.agent_id)
        return seen

    def _workflow_task_memory_store(self, agent_id: str) -> Any | None:
        try:
            agent = self._pool.get_agent(agent_id)
        except Exception:
            return None
        return getattr(agent, "memory_store", None) or getattr(agent, "store", None)

    def _require_workflow(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflow_registry.get(workflow_id)
        except KeyError as exc:
            raise WorkflowNotFoundError(
                f"workflow '{workflow_id}' is not registered"
            ) from exc

    @staticmethod
    def _serialize_workflow(workflow: WorkflowSpec) -> dict[str, Any]:
        def _task(task: Any) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "task_id": task.task_id,
                "agent_id": task.agent_id,
            }
            if task.structured_output is not None:
                entry["structured_output"] = dict(task.structured_output)
            return entry

        payload: dict[str, Any] = {
            "workflow_id": workflow.workflow_id,
            "tasks": [_task(task) for task in workflow.tasks],
        }
        if workflow.task_message is not None:
            payload["skill_name"] = workflow.task_message.skill_name
        if workflow.metadata:
            payload["metadata"] = dict(workflow.metadata)
        return payload


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


def _terminal_workflow_stream_event(
    status: Any | None,
    *,
    workflow_id: str,
    run_id: str,
) -> WorkflowStreamEvent | None:
    if status is None:
        return None
    mapped_status = _map_dbos_status(str(getattr(status, "status", "") or ""))
    if mapped_status == "completed":
        return WorkflowStreamEvent(event="", data={})
    if mapped_status in {"failed", "cancelled"}:
        error = _status_error(status) or f"workflow {mapped_status}"
        return WorkflowStreamEvent(
            event="workflow.error",
            data={
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": mapped_status,
                "error": error,
            },
        )
    return None


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


async def _sleep(duration: float) -> None:
    await asyncio.sleep(max(0.0, float(duration)))


async def _get_workflow_status_or_none(run_id: str) -> Any | None:
    try:
        return await workflow_dbos.get_workflow_status(run_id)
    except Exception:
        return None


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


def _runs_from_workflow_turns(
    turns: list[dict[str, Any]],
    *,
    workflow_id: str,
    limit: int,
    offset: int,
    sort_desc: bool,
) -> list[WorkflowRun]:
    latest_by_run: dict[str, dict[str, Any]] = {}
    for turn in turns:
        run_id = str(turn.get("run_id") or "").strip()
        if not run_id:
            continue
        current = latest_by_run.get(run_id)
        if current is None or _turn_sort_key(turn) > _turn_sort_key(current):
            latest_by_run[run_id] = turn

    ordered = sorted(
        latest_by_run.values(),
        key=_turn_sort_key,
        reverse=sort_desc,
    )
    sliced = ordered[max(0, int(offset)) : max(0, int(offset)) + max(1, int(limit))]
    return [
        WorkflowRun(
            run_id=str(turn["run_id"]),
            workflow_id=workflow_id,
            dedup_key=None,
            status="completed",
            created_at=float(turn.get("created_at") or 0.0),
            started_at=float(turn.get("created_at") or 0.0),
            finished_at=float(turn.get("created_at") or 0.0),
            error=None,
            output=None,
            summary=_workflow_run_summary_from_turn(turn),
        )
        for turn in sliced
    ]


def _turn_sort_key(turn: dict[str, Any]) -> tuple[float, str]:
    return float(turn.get("created_at") or 0.0), str(turn.get("turn_id") or "")


def _workflow_run_summary_from_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": str(turn.get("turn_id") or ""),
        "session_id": str(turn.get("session_id") or ""),
        "task_id": str(turn.get("task_id") or ""),
        "agent_id": str(turn.get("agent_id") or ""),
        "user_message": str(turn.get("user_message") or ""),
        "agent_response": str(turn.get("agent_response") or ""),
    }


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


def _parse_time_filter(value: str | None) -> float | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        return float(normalized)
    except ValueError:
        pass
    text = normalized
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()

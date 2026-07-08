"""Workflow orchestration service."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from . import dbos as workflow_dbos
from .registry import WorkflowRegistry
from .spec import WorkflowSpec
from .store import RUN_QUEUED, RUN_TERMINAL

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
    steps: list[dict[str, Any]] | None = None


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
    """Host-level workflow execution service.

    Step pipelines own their run history in the workflow store; strategy
    workflows (fan-out, branching) are projected from DBOS status.
    """

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

    def _workflow_store(self) -> Any | None:
        getter = getattr(self._pool, "get_workflow_store", None)
        return getter() if callable(getter) else None

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

        # Strategy workflows keep their own result surface (e.g. experiments) and
        # do not populate the workflow run store.
        if not workflow.steps:
            return []

        store = self._workflow_store()
        if store is None:
            return []
        records = await store.list_runs(
            resolved_workflow_id,
            status=_normalize_optional_text(status),
            start_time=_parse_time_filter(start_time),
            end_time=_parse_time_filter(end_time),
            limit=max(1, int(limit)),
            offset=max(0, int(offset)),
            sort_desc=bool(sort_desc),
        )
        return [_run_from_record(record) for record in records]

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
            status=RUN_QUEUED,
            created_at=time.time(),
        )

    async def get_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        workflow = self._require_workflow(resolved_workflow_id)
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")

        if workflow.steps:
            store = self._workflow_store()
            record = await store.get_run(resolved_run_id) if store is not None else None
            if record is not None:
                run = _run_from_record(record)
                steps = await store.get_run_steps(resolved_run_id)
                run.steps = [_step_to_dict(step) for step in steps]
                return run
            # The store row is written once the run starts executing; before that
            # the run is only visible as DBOS status. Fall through to it.

        status = await _get_workflow_status_or_none(resolved_run_id)
        if status is None:
            raise WorkflowNotFoundError(f"workflow run '{resolved_run_id}' was not found")
        return _run_from_status(
            status,
            workflow_id=resolved_workflow_id,
            dedup_key=_dedup_key_from_status(status, resolved_workflow_id),
        )

    async def resume_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        """Resume a failed step-pipeline run from its failed step (same run_id)."""
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        workflow = self._require_workflow(resolved_workflow_id)
        if not workflow.steps:
            raise ValueError("resume_run is only supported for step pipelines")
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")
        store = self._workflow_store()
        if store is None or await store.get_run(resolved_run_id) is None:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            )
        await workflow_dbos.resume_workflow_run(resolved_run_id)
        return await self.get_run(resolved_workflow_id, resolved_run_id)

    async def list_run_step_events(
        self, workflow_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        """Store-backed step audit trail for one run — visible for code steps."""
        resolved_workflow_id = str(workflow_id or "").strip()
        self._require_workflow(resolved_workflow_id)
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")
        store = self._workflow_store()
        if store is None:
            return []
        events = await store.list_step_events(resolved_run_id)
        return [
            {
                "run_id": event.run_id,
                "workflow_id": event.workflow_id,
                "step_id": event.step_id,
                "attempt": event.attempt,
                "event_type": event.event_type,
                "seq": event.seq,
                "at": event.at,
                "payload": event.payload,
            }
            for event in events
        ]

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

        if workflow.steps:
            store = self._workflow_store()
            if store is None or await store.get_run(resolved_run_id) is None:
                raise WorkflowNotFoundError(
                    f"workflow run '{resolved_run_id}' was not found"
                )
            return self._stream_step_run_events(
                resolved_workflow_id, resolved_run_id, store, poll_interval
            )

        # Strategy workflows: poll DBOS status to a terminal event.
        if await _get_workflow_status_or_none(resolved_run_id) is None:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            )
        return self._stream_strategy_run_events(
            resolved_workflow_id, resolved_run_id, poll_interval
        )

    async def _stream_step_run_events(
        self,
        workflow_id: str,
        run_id: str,
        store: Any,
        poll_interval: float,
    ) -> AsyncIterator[WorkflowStreamEvent]:
        emitted: set[tuple[str, int, str]] = set()
        while True:
            for event in await store.list_step_events(run_id):
                key = (event.step_id, event.seq, event.event_type)
                if key in emitted:
                    continue
                emitted.add(key)
                yield WorkflowStreamEvent(
                    event=event.event_type,
                    data={
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "step_id": event.step_id,
                        "attempt": event.attempt,
                        "seq": event.seq,
                        "at": event.at,
                        "payload": event.payload,
                    },
                )
            run = await store.get_run(run_id)
            if run is not None and run.status in RUN_TERMINAL:
                yield WorkflowStreamEvent(
                    event="workflow.completed"
                    if run.status == "completed"
                    else "workflow.error",
                    data={
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": run.status,
                        "result": run.result,
                        "error": run.error,
                    },
                )
                return
            yield WorkflowStreamEvent(event="", data={}, comment="keep-alive")
            await _sleep(poll_interval)

    async def _stream_strategy_run_events(
        self,
        workflow_id: str,
        run_id: str,
        poll_interval: float,
    ) -> AsyncIterator[WorkflowStreamEvent]:
        while True:
            status = await _get_workflow_status_or_none(run_id)
            mapped = _map_dbos_status(str(getattr(status, "status", "") or "")) if status else ""
            if mapped == "completed":
                yield WorkflowStreamEvent(
                    event="workflow.completed",
                    data={
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": mapped,
                        "output": _status_output(status),
                    },
                )
                return
            if mapped in {"failed", "cancelled"}:
                yield WorkflowStreamEvent(
                    event="workflow.error",
                    data={
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": mapped,
                        "error": _status_error(status) or f"workflow {mapped}",
                    },
                )
                return
            yield WorkflowStreamEvent(event="", data={}, comment="keep-alive")
            await _sleep(poll_interval)

    def _require_workflow(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflow_registry.get(workflow_id)
        except KeyError as exc:
            raise WorkflowNotFoundError(
                f"workflow '{workflow_id}' is not registered"
            ) from exc

    @staticmethod
    def _serialize_workflow(workflow: WorkflowSpec) -> dict[str, Any]:
        def _step(step: Any) -> dict[str, Any]:
            entry: dict[str, Any] = {"step_id": step.step_id, "kind": step.kind}
            if step.kind == "agent":
                entry["agent_id"] = getattr(step, "agent_id", None)
                skill_name = getattr(step, "skill_name", None)
                if skill_name:
                    entry["skill_name"] = skill_name
            output = step.output
            if isinstance(output, dict):
                entry["structured_output"] = dict(output)
            return entry

        payload: dict[str, Any] = {
            "workflow_id": workflow.workflow_id,
            "steps": [_step(step) for step in workflow.steps],
        }
        if workflow.strategy is not None and not workflow.steps:
            payload["strategy"] = type(workflow.strategy).__name__
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


def _run_from_record(record: Any) -> WorkflowRun:
    """Map a WorkflowStore run record to the public WorkflowRun projection."""
    return WorkflowRun(
        run_id=record.run_id,
        workflow_id=record.workflow_id,
        dedup_key=record.dedup_key,
        status=record.status,
        created_at=float(record.created_at or 0.0),
        started_at=record.started_at,
        finished_at=record.finished_at,
        error=record.error,
        output=record.result,
    )


def _step_to_dict(step: Any) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "ordinal": step.ordinal,
        "kind": step.kind,
        "status": step.status,
        "input_snapshot": step.input_snapshot,
        "output_snapshot": step.output_snapshot,
        "error": step.error,
        "attempt": step.attempt,
        "agent_request_id": step.agent_request_id,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


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


async def _sleep(duration: float) -> None:
    await asyncio.sleep(max(0.0, float(duration)))


async def _get_workflow_status_or_none(run_id: str) -> Any | None:
    try:
        return await workflow_dbos.get_workflow_status(run_id)
    except Exception:
        return None

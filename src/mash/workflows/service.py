"""Workflow orchestration service."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from pydantic import ValidationError

from mash.runtime.requests import validate_user_id

from .engine import WorkflowRunner
from .registry import WorkflowRegistry
from .runs import make_run_id
from .spec import WorkflowSpec
from .store import (
    RUN_COMPLETED,
    RUN_QUEUED,
    RUN_TERMINAL,
    RunDeduplicatedError,
    WorkflowRunRecord,
)

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool


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
    workflow_input: dict[str, Any] | None = None
    session_id: str | None = None
    user_id: str | None = None
    result: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None


@dataclass
class WorkflowStreamEvent:
    """One public workflow SSE event."""

    event: str
    data: dict[str, Any]
    comment: str | None = None


class WorkflowNotFoundError(LookupError):
    """Raised when a requested workflow is not registered."""


class WorkflowInputValidationError(ValueError):
    """Raised when workflow input does not satisfy the published input model."""

    def __init__(
        self,
        workflow_id: str,
        errors: list[dict[str, Any]],
    ) -> None:
        self.workflow_id = workflow_id
        self.errors = errors
        super().__init__(f"workflow '{workflow_id}' input validation failed")


class WorkflowUserRequiredError(ValueError):
    """Raised when a run of a workflow that uses agents names no ``user_id``."""

    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        super().__init__(
            f"workflow '{workflow_id}' submits agent requests, so a run requires a user_id"
        )


class WorkflowResumeNotSupportedError(RuntimeError):
    """Raised when a run cannot be resumed: it completed."""

    def __init__(self, run_id: str, status: str, message: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(message)


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

    A run's row is written when it is submitted, so every run, queued or not,
    is read from the workflow store.
    """

    def __init__(
        self,
        workflow_registry: WorkflowRegistry,
        pool: "Pool",
        *,
        runner_id: str,
    ) -> None:
        self._workflow_registry = workflow_registry
        self._pool = pool
        self._runner_id = str(runner_id or "").strip()
        if not self._runner_id:
            raise ValueError("runner_id is required")
        self._runner = WorkflowRunner(pool, runner_id=self._runner_id)

    async def recover(self) -> None:
        """Continue this deployment's unfinished runs. The host calls it on start."""
        await self._runner.recover()

    async def close(self) -> None:
        await self._runner.close()

    def _workflow_store(self) -> Any | None:
        getter = getattr(self._pool, "get_workflow_store", None)
        return getter() if callable(getter) else None

    async def list_workflows(self) -> list[dict[str, Any]]:
        workflows = self._workflow_registry.list()
        workflow_ids = [workflow.workflow_id for workflow in workflows]
        latest_runs: dict[str, Any] = {}
        history_available = False
        store = self._workflow_store()
        if store is not None:
            try:
                latest_runs = await store.get_latest_runs(workflow_ids)
                history_available = True
            except Exception:
                # Definition discovery remains usable when the optional run
                # history lookup is temporarily unavailable.
                latest_runs = {}
        return [
            self._serialize_workflow_summary(
                workflow,
                latest_run=latest_runs.get(workflow.workflow_id),
                history_available=history_available,
            )
            for workflow in workflows
        ]

    async def get_workflow_definition(self, workflow_id: str) -> dict[str, Any]:
        """Return the complete registered definition projection for one workflow."""
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        return self._serialize_workflow_definition(
            self._require_workflow(resolved_workflow_id)
        )

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
        self._require_workflow(resolved_workflow_id)

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
        user_id: str | None = None,
    ) -> WorkflowRun:
        """Start one run. ``user_id`` is required when the workflow uses agents."""
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        normalized_dedup_key = _normalize_optional_text(dedup_key)
        normalized_workflow_input = _normalize_workflow_input(workflow_input)
        normalized_session_id = _normalize_optional_text(session_id)
        workflow = self._require_workflow(resolved_workflow_id)
        if user_id is not None:
            validate_user_id(user_id)
        elif workflow.uses_agents:
            raise WorkflowUserRequiredError(resolved_workflow_id)
        if workflow.input_model is not None:
            try:
                workflow.input_model.model_validate(normalized_workflow_input)
            except ValidationError as exc:
                raise WorkflowInputValidationError(
                    resolved_workflow_id,
                    exc.errors(include_url=False, include_context=False),
                ) from exc
        store = self._workflow_store()
        if store is None:
            raise RuntimeError("the workflow store is not open; start the pool first")

        record = WorkflowRunRecord(
            run_id=make_run_id(self._runner_id, resolved_workflow_id),
            workflow_id=resolved_workflow_id,
            status=RUN_QUEUED,
            workflow_input=normalized_workflow_input,
            dedup_key=normalized_dedup_key,
            session_id=normalized_session_id,
            user_id=user_id,
            created_at=time.time(),
        )
        try:
            await store.create_run(record)
        except RunDeduplicatedError as exc:
            existing = await store.get_run(exc.run_id)
            raise DuplicateWorkflowRunError(
                resolved_workflow_id,
                normalized_dedup_key or "",
                _run_from_record(existing)
                if existing is not None
                else WorkflowRun(
                    run_id=exc.run_id,
                    workflow_id=resolved_workflow_id,
                    dedup_key=normalized_dedup_key,
                    status="running",
                    created_at=time.time(),
                ),
            ) from exc
        self._runner.start(record)
        return _run_from_record(record)

    async def get_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        self._require_workflow(resolved_workflow_id)
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")

        store = self._workflow_store()
        if store is not None:
            record = await store.get_run(resolved_run_id)
            if record is not None:
                run = _run_from_record(record)
                steps = await store.get_run_steps(resolved_run_id)
                run.steps = [_step_to_dict(step) for step in steps]
                return run
        raise WorkflowNotFoundError(f"workflow run '{resolved_run_id}' was not found")

    async def resume_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        """Continue a run from its completed steps.

        A failed or cancelled run goes back in the queue and reruns from the
        first step that did not complete. A queued or running run is left
        running. A completed run is rejected: see
        :class:`WorkflowResumeNotSupportedError`.
        """
        resolved_workflow_id = str(workflow_id or "").strip()
        if not resolved_workflow_id:
            raise ValueError("workflow_id is required")
        self._require_workflow(resolved_workflow_id)
        resolved_run_id = str(run_id or "").strip()
        if not resolved_run_id:
            raise ValueError("run_id is required")
        store = self._workflow_store()
        record = await store.get_run(resolved_run_id) if store is not None else None
        if record is None or record.workflow_id != resolved_workflow_id:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            )
        if record.status == RUN_COMPLETED:
            raise WorkflowResumeNotSupportedError(
                resolved_run_id,
                RUN_COMPLETED,
                f"workflow run '{resolved_run_id}' already completed "
                "successfully; start a new run instead",
            )
        if record.status in RUN_TERMINAL:
            await store.requeue_run(resolved_run_id)
        self._runner.start(record)
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
        self._require_workflow(resolved_workflow_id)
        if not resolved_run_id:
            raise ValueError("run_id is required")

        store = self._workflow_store()
        if store is None:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            )
        if await store.get_run(resolved_run_id) is None:
            raise WorkflowNotFoundError(
                f"workflow run '{resolved_run_id}' was not found"
            )
        return self._stream_step_run_events(
            resolved_workflow_id, resolved_run_id, store, poll_interval
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

    def _require_workflow(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflow_registry.get(workflow_id)
        except KeyError as exc:
            raise WorkflowNotFoundError(
                f"workflow '{workflow_id}' is not registered"
            ) from exc

    @staticmethod
    def _serialize_workflow_summary(
        workflow: WorkflowSpec,
        *,
        latest_run: Any | None,
        history_available: bool,
    ) -> dict[str, Any]:
        metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
        display_name = str(metadata.get("display_name") or "").strip()
        description = str(metadata.get("description") or "").strip()
        code_count = sum(1 for step in workflow.steps if step.kind == "code")
        agent_count = sum(1 for step in workflow.steps if step.kind == "agent")
        step_preview = []
        for ordinal, step in enumerate(workflow.steps[:4]):
            entry: dict[str, Any] = {
                "ordinal": ordinal,
                "step_id": step.step_id,
                "kind": step.kind,
            }
            if step.kind == "agent":
                entry["agent_id"] = getattr(step, "agent_id", None)
            step_preview.append(entry)
        return {
            "workflow_id": workflow.workflow_id,
            "display_name": display_name or workflow.workflow_id,
            "description": description,
            "mode": "pipeline",
            "step_count": len(workflow.steps),
            "step_kinds": {"code": code_count, "agent": agent_count},
            "step_preview": step_preview,
            "history_available": history_available,
            "latest_run": _run_record_summary(latest_run),
        }

    @staticmethod
    def _serialize_workflow_definition(workflow: WorkflowSpec) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        for ordinal, step in enumerate(workflow.steps):
            entry: dict[str, Any] = {
                "ordinal": ordinal,
                "step_id": step.step_id,
                "kind": step.kind,
                "input_schema": _schema_for(step.input),
                "output_schema": _schema_for(step.output),
                "timeout_s": step.timeout_s,
            }
            if step.kind == "code":
                entry["agent_ids"] = list(getattr(step, "agent_ids", ()))
            if step.kind == "agent":
                entry["agent_id"] = getattr(step, "agent_id", None)
                entry["skill_name"] = getattr(step, "skill_name", None)
            steps.append(entry)
        return {
            "workflow_id": workflow.workflow_id,
            "mode": "pipeline",
            "metadata": dict(workflow.metadata),
            "input_schema": _schema_for(workflow.input_model),
            "steps": steps,
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


def _schema_for(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    return dict(value.model_json_schema())


def _run_record_summary(record: Any | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "run_id": record.run_id,
        "status": record.status,
        "created_at": float(record.created_at or 0.0),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }


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
        workflow_input=dict(record.workflow_input),
        session_id=record.session_id,
        user_id=record.user_id,
        result=record.result,
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

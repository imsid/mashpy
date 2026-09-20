"""DBOS-backed workflow orchestration helpers."""

from __future__ import annotations

import importlib
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mash.runtime.engine.dbos import start_request_workflow
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import append_runtime_event
from mash.runtime.structured_output import normalize_structured_output_schema

from .spec import WorkflowSpec

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool


_WORKFLOW_NAME = "mash.workflow.execute"
_QUEUE_NAME = "mash.workflow.runs"
_QUEUE_CONCURRENCY = 8
_WORKFLOW_RUN_ID_PREFIX = "mw"
MASH_RUNNER_ID_ENV = "MASH_RUNNER_ID"
DEFAULT_RUNNER_ID = "default"
# A run id is colon-delimited (``mw:<runner>:<workflow>:<token>``), so the
# runner id may not contain a colon.
_RUNNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WORKFLOW_TASK_STRUCTURED_OUTPUT = {
    "title": "WorkflowTaskState",
    "type": "object",
    "properties": {},
    "required": [],
    # Provider-native structured output requires a closed object schema for
    # Anthropic. Agent steps should declare an output model/schema for non-empty state.
    "additionalProperties": False,
}


@dataclass
class _DBOSWorkflowState:
    registered_workflow: Any = None
    queue: Any = None
    runner_registry: dict[str, "Pool"] = field(default_factory=dict)


_STATE = _DBOSWorkflowState()


def _load_dbos_api() -> tuple[Any, Any, Any, Any]:
    try:
        module = importlib.import_module("dbos")
        error_module = importlib.import_module("dbos._error")
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise RuntimeError(
            "dbos is required for workflow orchestration. Install mashpy with DBOS dependencies."
        ) from exc

    dbos_class = getattr(module, "DBOS", None)
    set_workflow_id = getattr(module, "SetWorkflowID", None)
    set_enqueue_options = getattr(module, "SetEnqueueOptions", None)
    dedup_error = getattr(error_module, "DBOSQueueDeduplicatedError", None)
    if (
        dbos_class is None
        or set_workflow_id is None
        or set_enqueue_options is None
        or dedup_error is None
    ):
        raise RuntimeError("dbos module is missing required workflow APIs")
    return dbos_class, set_workflow_id, set_enqueue_options, dedup_error


def _compact_token(num_bytes: int) -> str:
    return secrets.token_urlsafe(num_bytes).rstrip("=")


def resolve_runner_id(explicit_value: str | None = None) -> str:
    """Resolve this deployment's durable workflow runner identity.

    The runner id is written into every queued run's durable arguments and is
    how recovery finds the pool that owns a persisted run. It must therefore
    be a property of the *deployment*, stable across restarts, not of the
    process: a per-process token leaves every run from a previous process
    unresolvable after a restart.

    Deployments that share one database give each pool its own id (via
    ``MASH_RUNNER_ID`` or ``Pool(runner_id=...)``) so their runs stay isolated;
    a single deployment keeps the default and recovers its own runs.
    """
    for candidate in (explicit_value, os.getenv(MASH_RUNNER_ID_ENV)):
        value = str(candidate or "").strip()
        if not value:
            continue
        if not _RUNNER_ID_PATTERN.match(value):
            raise ValueError(
                f"runner_id '{value}' is invalid: use letters, digits, '.', '_' "
                "or '-', starting with a letter or digit"
            )
        return value
    return DEFAULT_RUNNER_ID


def workflow_run_id_prefix(runner_id: str, workflow_id: str) -> str:
    return f"{_WORKFLOW_RUN_ID_PREFIX}:{runner_id}:{workflow_id}:"


def make_run_id(runner_id: str, workflow_id: str) -> str:
    return f"{workflow_run_id_prefix(runner_id, workflow_id)}{_compact_token(12)}"


def register_runner(runner_id: str, pool: "Pool") -> None:
    resolved = str(runner_id or "").strip()
    if not resolved:
        raise ValueError("runner_id is required")
    existing = _STATE.runner_registry.get(resolved)
    if existing is not None and existing is not pool:
        # Runner ids are durable addresses. Letting a second pool take over an
        # id would route another deployment's persisted runs into this one.
        raise ValueError(
            f"workflow runner '{resolved}' is already registered to a different "
            f"pool; give each pool its own id via {MASH_RUNNER_ID_ENV} or "
            "Pool(runner_id=...)"
        )
    _STATE.runner_registry[resolved] = pool


def unregister_runner(runner_id: str, pool: "Pool") -> None:
    existing = _STATE.runner_registry.get(runner_id)
    if existing is pool:
        _STATE.runner_registry.pop(runner_id, None)


def require_runner(runner_id: str) -> "Pool":
    pool = _STATE.runner_registry.get(runner_id)
    if pool is None:
        raise RuntimeError(f"workflow runner '{runner_id}' is not registered")
    return pool


def register_workflow(dbos_class: Any) -> None:
    """Register the workflow entrypoint. Must run before ``DBOS.launch()``."""
    if _STATE.registered_workflow is not None:
        return

    async def _workflow(
        runner_id: str,
        workflow_id: str,
        run_id: str,
        workflow_input: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await execute_registered_workflow(
            runner_id,
            workflow_id,
            run_id,
            workflow_input=workflow_input,
            session_id=session_id,
        )

    _STATE.registered_workflow = dbos_class.workflow(name=_WORKFLOW_NAME)(_workflow)


async def ensure_queue_registered(dbos_class: Any) -> None:
    """Declare the run queue in the system database. Must run after launch.

    Queues are rows in the DBOS system database, so the queue can only be
    declared once ``DBOS.launch()`` has opened it. Registration is an upsert
    keyed by queue name, so every process in a deployment declares the same
    queue on startup and converges on one row.
    """
    if _STATE.queue is not None:
        return
    _STATE.queue = await dbos_class.register_queue_async(
        _QUEUE_NAME, global_concurrency=_QUEUE_CONCURRENCY
    )


async def start_workflow_run(
    *,
    database_url: str,
    runner_id: str,
    workflow: WorkflowSpec,
    dedup_key: str | None,
    workflow_input: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    # Deferred: mash.runtime.engine.dbos imports this module back.
    from mash.runtime.engine.dbos import ensure_dbos_ready  # pylint: disable=import-outside-toplevel

    await ensure_dbos_ready(database_url)
    dbos_class, set_workflow_id, set_enqueue_options, dedup_error = _load_dbos_api()
    register_workflow(dbos_class)

    if _STATE.registered_workflow is None or _STATE.queue is None:
        raise RuntimeError("DBOS workflow orchestration is not registered")

    run_id = make_run_id(runner_id, workflow.workflow_id)
    normalized_workflow_input = _normalize_workflow_input(workflow_input)
    normalized_session_id = str(session_id).strip() if session_id else None
    try:
        with set_workflow_id(run_id):
            if dedup_key is None:
                handle = await _STATE.queue.enqueue_async(
                    _STATE.registered_workflow,
                    runner_id,
                    workflow.workflow_id,
                    run_id,
                    normalized_workflow_input,
                    normalized_session_id,
                )
            else:
                with set_enqueue_options(
                    deduplication_id=f"{workflow.workflow_id}:{dedup_key}"
                ):
                    handle = await _STATE.queue.enqueue_async(
                        _STATE.registered_workflow,
                        runner_id,
                        workflow.workflow_id,
                        run_id,
                        normalized_workflow_input,
                        normalized_session_id,
                    )
    except dedup_error as exc:
        existing_run_id = str(getattr(exc, "workflow_id", "") or "")
        raise WorkflowDeduplicatedError(existing_run_id) from exc
    return str(handle.get_workflow_id())


async def get_workflow_status(run_id: str) -> Any | None:
    dbos_class, _, _, _ = _load_dbos_api()
    return await dbos_class.get_workflow_status_async(run_id)


async def resume_workflow_run(run_id: str) -> str:
    """Re-drive an interrupted DBOS workflow run from its last checkpoint.

    DBOS replays completed steps from their memoized outputs and continues from
    where the run stopped, under the same ``run_id``. Only non-terminal runs
    transition: ``resume_workflows`` updates rows whose status is outside
    ``SUCCESS`` and ``ERROR``, so callers must reject terminal runs before
    calling this rather than report the resulting no-op as a resume. See
    ``WorkflowService.resume_run``.
    """
    resolved = str(run_id or "").strip()
    if not resolved:
        raise ValueError("run_id is required")
    dbos_class, _, _, _ = _load_dbos_api()
    resume = getattr(dbos_class, "resume_workflow_async", None)
    if resume is None:
        raise RuntimeError("dbos does not support resume_workflow_async")
    handle = await resume(resolved)
    workflow_id = getattr(handle, "get_workflow_id", None)
    if callable(workflow_id):
        return str(workflow_id())
    return resolved


async def execute_registered_workflow(
    runner_id: str,
    workflow_id: str,
    run_id: str,
    *,
    workflow_input: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    pool = require_runner(runner_id)
    workflow = pool.get_workflow_registry().get(workflow_id)
    # Lazy import: engine imports helpers from this module.
    from .engine import (  # pylint: disable=import-outside-toplevel
        FORWARD_PIPELINE_STRATEGY,
        WorkflowExecutionContext,
    )

    ctx = WorkflowExecutionContext(
        runner_id=runner_id,
        workflow=workflow,
        run_id=run_id,
        workflow_input=_normalize_workflow_input(workflow_input),
        session_id=session_id,
    )
    return await FORWARD_PIPELINE_STRATEGY.run(ctx)


async def post_inline_agent_request(
    runner_id: str,
    *,
    agent_id: str,
    message: str,
    structured_output: dict[str, Any] | None,
    workflow_id: str,
    workflow_run_id: str,
    task_id: str,
    session_id: str,
    host_snapshot: dict[str, Any] | None = None,
) -> str:
    """Start one agent request inline from within a workflow and return its id.

    Used by the forward engine and orchestration-heavy code steps to run an
    agent request as a child of the workflow. Pair with
    ``collect_terminal_payload`` to await the result.
    """
    pool = require_runner(runner_id)
    runtime = _resolve_inline_runtime(pool, agent_id)
    normalized = (
        normalize_structured_output_schema(structured_output)
        if isinstance(structured_output, dict)
        else _WORKFLOW_TASK_STRUCTURED_OUTPUT
    )
    if runtime is not None:
        return await _execute_inline_task_request(
            runtime,
            agent_id=agent_id,
            message=message,
            session_id=session_id,
            structured_output=normalized,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            task_id=task_id,
            host_snapshot=host_snapshot,
        )
    client = pool.get_client(agent_id)
    return await client.post_request(
        message,
        session_id=session_id,
        structured_output=normalized,
    )


def _resolve_inline_runtime(pool: "Pool", agent_id: str) -> Any | None:
    get_agent = getattr(pool, "get_agent", None)
    if not callable(get_agent):
        return None
    try:
        return get_agent(agent_id)
    except Exception:
        return None


async def _execute_inline_task_request(
    runtime: Any,
    *,
    agent_id: str,
    message: str,
    session_id: str,
    structured_output: dict[str, Any],
    workflow_id: str,
    workflow_run_id: str,
    task_id: str,
    host_snapshot: dict[str, Any] | None = None,
) -> str:
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mash.workflow.task:{agent_id}:{workflow_run_id}:{task_id}",
        )
    )
    request_metadata: dict[str, Any] = {
        "structured_output_request": dict(structured_output),
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "task_id": task_id,
    }
    if host_snapshot is not None:
        # Mirrors runtime.requests.submit_request: run the request against the
        # given host composition (its subagents), not just the bare agent.
        request_metadata["host"] = dict(host_snapshot)
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            session_id=session_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
            dedupe_key="request.accepted",
            payload={
                "message": message,
                "initial_session_id": session_id,
                "request_metadata": dict(request_metadata),
            },
        ),
    )
    def _resolve(requested_agent_id: str) -> Any:
        if requested_agent_id != agent_id:
            raise RuntimeError(f"runtime '{requested_agent_id}' is not registered")
        return runtime

    await start_request_workflow(
        agent_id,
        request_id,
        message,
        session_id,
        request_metadata,
        require_runtime_fallback=_resolve,
    )
    return request_id


async def _collect_terminal_payload(
    runner_id: str,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    pool = require_runner(runner_id)
    client = pool.get_client(agent_id)
    async for event in client.stream_response(request_id):
        event_name = str(event.get("event") or "")
        payload = event.get("data")
        if event_name == "request.completed":
            if not isinstance(payload, dict):
                raise RuntimeError("completed task response must be an object")
            return payload
        if event_name == "request.cancelled":
            raise RuntimeError("workflow task request was cancelled")
        if event_name == "request.error":
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or payload.get("status")
                raise RuntimeError(str(message or "workflow task failed"))
            raise RuntimeError("workflow task failed")
    raise RuntimeError("workflow task stream ended without a terminal event")


# Public alias so code steps await request results through the same terminal
# collector the forward engine uses.
collect_terminal_payload = _collect_terminal_payload


def _run_session_id(run_id: str) -> str:
    """Deterministic session id for one workflow run.

    Placeholder until a caller (e.g. the REPL) session is threaded through:
    every task of the run shares it, and it is stable across DBOS retries.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mash.workflow.run:{run_id}"))


def _normalize_workflow_input(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("workflow_input must be a JSON object")
    return dict(value)


class WorkflowDeduplicatedError(RuntimeError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__("workflow run was deduplicated")


# Public accessor for extensions that need the same centralized DBOS loader.
load_dbos_api = _load_dbos_api


__all__ = [
    "WorkflowDeduplicatedError",
    "collect_terminal_payload",
    "ensure_queue_registered",
    "execute_registered_workflow",
    "get_workflow_status",
    "load_dbos_api",
    "make_run_id",
    "post_inline_agent_request",
    "register_runner",
    "register_workflow",
    "require_runner",
    "resolve_runner_id",
    "start_workflow_run",
    "unregister_runner",
    "workflow_run_id_prefix",
]

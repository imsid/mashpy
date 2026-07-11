"""DBOS-backed workflow orchestration helpers."""

from __future__ import annotations

import importlib
import secrets
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mash.runtime.engine.dbos import start_request_workflow
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import append_runtime_event
from mash.runtime.structured_output import normalize_structured_output_schema

from .spec import WorkflowSpec
from .strategy import WorkflowExecutionContext

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentPool


_WORKFLOW_NAME = "mash.workflow.execute"
_QUEUE_NAME = "mash.workflow.runs"
_WORKFLOW_RUN_ID_PREFIX = "mw"
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
    runner_registry: dict[str, "AgentPool"] = field(default_factory=dict)


_STATE = _DBOSWorkflowState()


def _load_dbos_api() -> tuple[Any, Any, Any, Any, Any]:
    try:
        module = importlib.import_module("dbos")
        error_module = importlib.import_module("dbos._error")
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise RuntimeError(
            "dbos is required for workflow orchestration. Install mashpy with DBOS dependencies."
        ) from exc

    dbos_class = getattr(module, "DBOS", None)
    queue_class = getattr(module, "Queue", None)
    set_workflow_id = getattr(module, "SetWorkflowID", None)
    set_enqueue_options = getattr(module, "SetEnqueueOptions", None)
    dedup_error = getattr(error_module, "DBOSQueueDeduplicatedError", None)
    if (
        dbos_class is None
        or queue_class is None
        or set_workflow_id is None
        or set_enqueue_options is None
        or dedup_error is None
    ):
        raise RuntimeError("dbos module is missing required workflow APIs")
    return dbos_class, queue_class, set_workflow_id, set_enqueue_options, dedup_error


def _compact_token(num_bytes: int) -> str:
    return secrets.token_urlsafe(num_bytes).rstrip("=")


def make_runner_id() -> str:
    return f"r_{_compact_token(9)}"


def workflow_run_id_prefix(runner_id: str, workflow_id: str) -> str:
    return f"{_WORKFLOW_RUN_ID_PREFIX}:{runner_id}:{workflow_id}:"


def make_run_id(runner_id: str, workflow_id: str) -> str:
    return f"{workflow_run_id_prefix(runner_id, workflow_id)}{_compact_token(12)}"


def register_runner(runner_id: str, pool: "AgentPool") -> None:
    resolved = str(runner_id or "").strip()
    if not resolved:
        raise ValueError("runner_id is required")
    _STATE.runner_registry[resolved] = pool


def unregister_runner(runner_id: str, pool: "AgentPool") -> None:
    existing = _STATE.runner_registry.get(runner_id)
    if existing is pool:
        _STATE.runner_registry.pop(runner_id, None)


def require_runner(runner_id: str) -> "AgentPool":
    pool = _STATE.runner_registry.get(runner_id)
    if pool is None:
        raise RuntimeError(f"workflow runner '{runner_id}' is not registered")
    return pool


def register_workflow(dbos_class: Any) -> None:
    if _STATE.registered_workflow is not None:
        # Generic workflow already registered; still give strategies a chance to
        # register (idempotent) in case a pool's workflows appeared afterward.
        _register_strategies(dbos_class)
        return
    _, queue_class, _, _, _ = _load_dbos_api()
    _STATE.queue = queue_class(_QUEUE_NAME, concurrency=8)

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
    _register_strategies(dbos_class)


def _register_strategies(dbos_class: Any) -> None:
    """Let each registered workflow's strategy register its DBOS objects.

    Runs before ``DBOS.launch()`` (register_workflow is called from
    ``ensure_dbos_ready`` prior to launch). Driven by the strategies the pools'
    ``WorkflowSpec``s already carry, so this module never imports any concrete
    strategy. Strategy ``register`` implementations must be idempotent.
    """
    for pool in list(_STATE.runner_registry.values()):
        for workflow in pool.get_workflow_registry().list():
            strategy = workflow.strategy
            if strategy is not None:
                strategy.register(dbos_class)


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
    dbos_class, _, set_workflow_id, set_enqueue_options, dedup_error = _load_dbos_api()
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
    dbos_class, _, _, _, _ = _load_dbos_api()
    return await dbos_class.get_workflow_status_async(run_id)


async def resume_workflow_run(run_id: str) -> str:
    """Resume a failed/interrupted DBOS workflow run from its failed step.

    DBOS replays completed steps from their memoized outputs and re-drives from
    the point of failure. Returns the same ``run_id``.
    """
    resolved = str(run_id or "").strip()
    if not resolved:
        raise ValueError("run_id is required")
    dbos_class, _, _, _, _ = _load_dbos_api()
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
    ctx = WorkflowExecutionContext(
        runner_id=runner_id,
        workflow=workflow,
        run_id=run_id,
        workflow_input=_normalize_workflow_input(workflow_input),
        session_id=session_id,
    )
    strategy = workflow.strategy
    if strategy is None:
        if not workflow.steps:
            raise RuntimeError(
                f"workflow '{workflow_id}' has neither steps nor a strategy"
            )
        # Lazy import: engine imports helpers from this module.
        from .engine import FORWARD_PIPELINE_STRATEGY  # pylint: disable=import-outside-toplevel

        strategy = FORWARD_PIPELINE_STRATEGY
    return await strategy.run(ctx)


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


def _resolve_inline_runtime(pool: "AgentPool", agent_id: str) -> Any | None:
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
        if event_name == "request.error":
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or payload.get("status")
                raise RuntimeError(str(message or "workflow task failed"))
            raise RuntimeError("workflow task failed")
    raise RuntimeError("workflow task stream ended without a terminal event")


# Public alias so code steps and strategies await request results through the
# same terminal collector the forward engine uses.
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
    "execute_registered_workflow",
    "get_workflow_status",
    "load_dbos_api",
    "make_runner_id",
    "make_run_id",
    "post_inline_agent_request",
    "register_runner",
    "register_workflow",
    "require_runner",
    "start_workflow_run",
    "unregister_runner",
    "workflow_run_id_prefix",
]

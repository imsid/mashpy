"""DBOS-backed workflow orchestration helpers."""

from __future__ import annotations

import importlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mash.runtime.engine.workflow import execute_request_workflow, workflow_id_for
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import append_runtime_event

from .spec import WorkflowSpec

if TYPE_CHECKING:
    from mash.runtime.host.host import AgentHost


_WORKFLOW_NAME = "mash.workflow.execute"
_QUEUE_NAME = "mash.workflow.runs"
_WORKFLOW_RUN_ID_PREFIX = "mw"


@dataclass
class _DBOSWorkflowState:
    registered_workflow: Any = None
    queue: Any = None
    host_registry: dict[str, "AgentHost"] = field(default_factory=dict)


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


def make_host_id() -> str:
    return f"h_{_compact_token(9)}"


def workflow_run_id_prefix(host_id: str, workflow_id: str) -> str:
    return f"{_WORKFLOW_RUN_ID_PREFIX}:{host_id}:{workflow_id}:"


def make_run_id(host_id: str, workflow_id: str) -> str:
    return f"{workflow_run_id_prefix(host_id, workflow_id)}{_compact_token(12)}"


def register_host(host_id: str, host: "AgentHost") -> None:
    resolved = str(host_id or "").strip()
    if not resolved:
        raise ValueError("host_id is required")
    _STATE.host_registry[resolved] = host


def unregister_host(host_id: str, host: "AgentHost") -> None:
    existing = _STATE.host_registry.get(host_id)
    if existing is host:
        _STATE.host_registry.pop(host_id, None)


def require_host(host_id: str) -> "AgentHost":
    host = _STATE.host_registry.get(host_id)
    if host is None:
        raise RuntimeError(f"workflow host '{host_id}' is not registered")
    return host


def register_workflow(dbos_class: Any) -> None:
    if _STATE.registered_workflow is not None:
        return
    _, queue_class, _, _, _ = _load_dbos_api()
    _STATE.queue = queue_class(_QUEUE_NAME, concurrency=8)

    async def _workflow(
        host_id: str,
        workflow_id: str,
        run_id: str,
        workflow_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await execute_registered_workflow(
            host_id,
            workflow_id,
            run_id,
            workflow_input=workflow_input,
        )

    _STATE.registered_workflow = dbos_class.workflow(name=_WORKFLOW_NAME)(_workflow)


async def start_workflow_run(
    *,
    database_url: str,
    host_id: str,
    workflow: WorkflowSpec,
    dedup_key: str | None,
    workflow_input: dict[str, Any] | None = None,
) -> str:
    from mash.runtime.engine.dbos import ensure_dbos_ready

    await ensure_dbos_ready(database_url)
    dbos_class, _, set_workflow_id, set_enqueue_options, dedup_error = _load_dbos_api()
    register_workflow(dbos_class)

    if _STATE.registered_workflow is None or _STATE.queue is None:
        raise RuntimeError("DBOS workflow orchestration is not registered")

    run_id = make_run_id(host_id, workflow.workflow_id)
    normalized_workflow_input = _normalize_workflow_input(workflow_input)
    try:
        with set_workflow_id(run_id):
            if dedup_key is None:
                handle = await _STATE.queue.enqueue_async(
                    _STATE.registered_workflow,
                    host_id,
                    workflow.workflow_id,
                    run_id,
                    normalized_workflow_input,
                )
            else:
                with set_enqueue_options(
                    deduplication_id=f"{workflow.workflow_id}:{dedup_key}"
                ):
                    handle = await _STATE.queue.enqueue_async(
                        _STATE.registered_workflow,
                        host_id,
                        workflow.workflow_id,
                        run_id,
                        normalized_workflow_input,
                    )
    except dedup_error as exc:
        existing_run_id = str(getattr(exc, "workflow_id", "") or "")
        raise WorkflowDeduplicatedError(existing_run_id) from exc
    return str(handle.get_workflow_id())


async def get_workflow_status(run_id: str) -> Any | None:
    dbos_class, _, _, _, _ = _load_dbos_api()
    return await dbos_class.get_workflow_status_async(run_id)


async def execute_registered_workflow(
    host_id: str,
    workflow_id: str,
    run_id: str,
    *,
    workflow_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dbos_class, _, _, _, _ = _load_dbos_api()
    host = require_host(host_id)
    workflow = host.get_workflow_registry().get(workflow_id)
    task_requests: dict[str, str] = {}
    task_states: dict[str, dict[str, Any]] = {}
    normalized_workflow_input = _normalize_workflow_input(workflow_input)

    for task in workflow.tasks:
        previous_state = await load_previous_task_state(
            host_id=host_id,
            workflow_id=workflow.workflow_id,
            task_id=task.task_id,
            current_run_id=run_id,
        )
        # Starting a task request through the normal in-process client would
        # enqueue a child DBOS workflow from inside this host workflow. DBOS can
        # reject that, so workflow tasks enter the request workflow body inline.
        request_id = await _post_task_request(
            host_id,
            workflow.workflow_id,
            run_id,
            task.task_id,
            task.agent_id,
            normalized_workflow_input,
            previous_state,
        )
        payload = await dbos_class.run_step_async(
            {"name": f"{task.task_id}.request.await"},
            _collect_terminal_payload,
            host_id,
            task.agent_id,
            request_id,
        )
        next_state = await dbos_class.run_step_async(
            {"name": f"{task.task_id}.state.extract"},
            _extract_task_state,
            payload,
        )
        task_requests[task.task_id] = request_id
        task_states[task.task_id] = next_state

    return {
        "workflow_id": workflow.workflow_id,
        "run_id": run_id,
        "completed_at": time.time(),
        "task_requests": task_requests,
        "task_states": task_states,
    }


async def load_previous_task_state(
    *,
    host_id: str,
    workflow_id: str,
    task_id: str,
    current_run_id: str,
) -> dict[str, Any]:
    dbos_class, _, _, _, _ = _load_dbos_api()
    statuses = await dbos_class.list_workflows_async(
        name=_WORKFLOW_NAME,
        workflow_id_prefix=workflow_run_id_prefix(host_id, workflow_id),
        status="SUCCESS",
        sort_desc=True,
        limit=20,
        load_input=False,
        load_output=True,
    )
    for status in statuses:
        if getattr(status, "workflow_id", None) == current_run_id:
            continue
        output = getattr(status, "output", None)
        if not isinstance(output, dict):
            continue
        task_states = output.get("task_states")
        if not isinstance(task_states, dict):
            continue
        state = task_states.get(task_id)
        if isinstance(state, dict):
            return dict(state)
    return {}


async def _post_task_request(
    host_id: str,
    workflow_id: str,
    run_id: str,
    task_id: str,
    agent_id: str,
    workflow_input: dict[str, Any],
    task_state: dict[str, Any],
) -> str:
    host = require_host(host_id)
    client = host.get_client(agent_id)
    session_id = _task_session_id(
        workflow_id=workflow_id,
        task_id=task_id,
        run_id=run_id,
    )
    message = _build_task_message(
        workflow_id=workflow_id,
        workflow_run_id=run_id,
        task_id=task_id,
        workflow_input=workflow_input,
        task_state=task_state,
    )
    runtime = _resolve_inline_runtime(host, agent_id)
    if runtime is not None:
        return await _execute_inline_task_request(
            runtime,
            agent_id=agent_id,
            message=message,
            session_id=session_id,
        )
    return await client.post_request(
        message,
        session_id=session_id,
    )


def _resolve_inline_runtime(host: "AgentHost", agent_id: str) -> Any | None:
    get_agent = getattr(host, "get_agent", None)
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
) -> str:
    request_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"mash.workflow.task:{agent_id}:{session_id}")
    )
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            session_id=session_id,
            event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
            dedupe_key="request.accepted",
            payload={
                "workflow_id": workflow_id_for(runtime.app_id, request_id),
                "message": message,
                "initial_session_id": session_id,
                "request_metadata": {},
            },
        ),
    )
    await execute_request_workflow(
        agent_id,
        request_id,
        message,
        session_id,
        {},
        require_runtime=_inline_runtime_resolver(runtime, agent_id),
    )
    return request_id


def _inline_runtime_resolver(runtime: Any, agent_id: str) -> Any:
    def _resolve(requested_agent_id: str) -> Any:
        if requested_agent_id != agent_id:
            raise RuntimeError(f"runtime '{requested_agent_id}' is not registered")
        return runtime

    return _resolve


async def _collect_terminal_payload(
    host_id: str,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    host = require_host(host_id)
    client = host.get_client(agent_id)
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


def _extract_task_state(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("workflow task completed without a response payload")
    text = response.get("text")
    if not isinstance(text, str):
        raise RuntimeError("workflow task response text is required")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"workflow task output must be valid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("workflow task output must be a JSON object")
    return dict(decoded)


def _build_task_message(
    *,
    workflow_id: str,
    workflow_run_id: str,
    task_id: str,
    workflow_input: dict[str, Any],
    task_state: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "workflow_id": workflow_id,
            "workflow_run_id": workflow_run_id,
            "task_id": task_id,
            "workflow_input": dict(workflow_input),
            "task_state": dict(task_state),
        },
        ensure_ascii=True,
    )


def _task_session_id(*, workflow_id: str, task_id: str, run_id: str) -> str:
    return f"workflow:{workflow_id}:task:{task_id}:run:{run_id}"


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


__all__ = [
    "WorkflowDeduplicatedError",
    "execute_registered_workflow",
    "get_workflow_status",
    "load_previous_task_state",
    "make_host_id",
    "make_run_id",
    "register_host",
    "register_workflow",
    "require_host",
    "start_workflow_run",
    "unregister_host",
    "workflow_run_id_prefix",
]

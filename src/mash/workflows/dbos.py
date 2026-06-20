"""DBOS-backed workflow orchestration helpers."""

from __future__ import annotations

import importlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mash.runtime.engine.dbos import start_request_workflow
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.requests import append_runtime_event
from mash.runtime.structured_output import normalize_structured_output_schema

from .spec import WorkflowSpec, WorkflowTaskMessageSpec

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
    # Anthropic. Hosts should set TaskSpec.structured_output for non-empty task state.
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


async def start_workflow_run(
    *,
    database_url: str,
    runner_id: str,
    workflow: WorkflowSpec,
    dedup_key: str | None,
    workflow_input: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> str:
    from mash.runtime.engine.dbos import ensure_dbos_ready

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


async def execute_registered_workflow(
    runner_id: str,
    workflow_id: str,
    run_id: str,
    *,
    workflow_input: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    dbos_class, _, _, _, _ = _load_dbos_api()
    pool = require_runner(runner_id)
    workflow = pool.get_workflow_registry().get(workflow_id)
    task_requests: dict[str, str] = {}
    task_states: dict[str, dict[str, Any]] = {}
    normalized_workflow_input = _normalize_workflow_input(workflow_input)

    for task in workflow.tasks:
        previous_state = await load_previous_task_state(
            runner_id=runner_id,
            workflow_id=workflow.workflow_id,
            task_id=task.task_id,
            current_run_id=run_id,
        )
        # Starting a task request through the normal in-process client would
        # enqueue a child DBOS workflow from inside this host workflow. DBOS can
        # reject that, so workflow tasks enter the request workflow body inline.
        request_id = await _post_task_request(
            runner_id,
            workflow.workflow_id,
            run_id,
            task.task_id,
            task.agent_id,
            normalized_workflow_input,
            previous_state,
            workflow.task_message,
            task.structured_output,
            session_id=session_id,
        )
        payload = await dbos_class.run_step_async(
            {"name": f"{task.task_id}.request.await"},
            _collect_terminal_payload,
            runner_id,
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
    runner_id: str,
    workflow_id: str,
    task_id: str,
    current_run_id: str,
) -> dict[str, Any]:
    dbos_class, _, _, _, _ = _load_dbos_api()
    statuses = await dbos_class.list_workflows_async(
        name=_WORKFLOW_NAME,
        workflow_id_prefix=workflow_run_id_prefix(runner_id, workflow_id),
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
    runner_id: str,
    workflow_id: str,
    run_id: str,
    task_id: str,
    agent_id: str,
    workflow_input: dict[str, Any],
    task_state: dict[str, Any],
    task_message: WorkflowTaskMessageSpec | None,
    structured_output: dict[str, Any] | None,
    *,
    session_id: str | None = None,
) -> str:
    pool = require_runner(runner_id)
    client = pool.get_client(agent_id)
    # A workflow run executes under one session: the caller's session when one is
    # threaded through (e.g. the REPL), otherwise a fresh per-run session. The run
    # is a trace in that session, tagged with workflow_run_id, not a session of
    # its own.
    session_id = session_id or _run_session_id(run_id)
    message = _build_task_message(
        workflow_id=workflow_id,
        workflow_run_id=run_id,
        task_id=task_id,
        workflow_input=workflow_input,
        task_state=task_state,
        task_message=task_message,
    )
    runtime = _resolve_inline_runtime(pool, agent_id)
    task_structured_output = (
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
            structured_output=task_structured_output,
            workflow_id=workflow_id,
            workflow_run_id=run_id,
            task_id=task_id,
        )
    return await client.post_request(
        message,
        session_id=session_id,
        structured_output=task_structured_output,
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
) -> str:
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mash.workflow.task:{agent_id}:{workflow_run_id}:{task_id}",
        )
    )
    request_metadata = {
        "structured_output_request": dict(structured_output),
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "task_id": task_id,
    }
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


def _extract_task_state(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("workflow task completed without a response payload")
    structured_output = response.get("structured_output")
    if not isinstance(structured_output, dict):
        raise RuntimeError("workflow task response structured_output is required")
    return dict(structured_output)


def _build_task_message(
    *,
    workflow_id: str,
    workflow_run_id: str,
    task_id: str,
    workflow_input: dict[str, Any],
    task_state: dict[str, Any],
    task_message: WorkflowTaskMessageSpec | None = None,
) -> str:
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "task_id": task_id,
        "workflow_input": dict(workflow_input),
        "task_state": dict(task_state),
    }
    if task_message is not None:
        skill_name = str(task_message.skill_name or "").strip()
        payload["skill_name"] = skill_name
        payload["workflow_task_instructions"] = [
            (
                f"Your first action must be calling the Skill tool with "
                f"arguments {{\"name\": \"{skill_name}\"}}."
            ),
            "After the Skill tool returns, follow the loaded skill instructions.",
            "Execute only the task identified by task_id.",
        ]
    return json.dumps(payload, ensure_ascii=True)


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


__all__ = [
    "WorkflowDeduplicatedError",
    "execute_registered_workflow",
    "get_workflow_status",
    "load_previous_task_state",
    "make_runner_id",
    "make_run_id",
    "register_runner",
    "register_workflow",
    "require_runner",
    "start_workflow_run",
    "unregister_runner",
    "workflow_run_id_prefix",
]

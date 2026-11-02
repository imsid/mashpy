"""Agent requests started by workflow steps.

An agent step, and a code step that starts agent requests itself, submits
under a request id fixed by the run, the step and the step's attempt, so a
step that runs again after a crash reattaches to the request it started.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mash.runtime.structured_output import normalize_structured_output_schema

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool


_WORKFLOW_TASK_STRUCTURED_OUTPUT = {
    "title": "WorkflowTaskState",
    "type": "object",
    "properties": {},
    "required": [],
    # Provider-native structured output requires a closed object schema for
    # Anthropic. Agent steps should declare an output model/schema for non-empty state.
    "additionalProperties": False,
}


def agent_step_request_id(workflow_run_id: str, task_id: str, attempt: int = 1) -> str:
    """The request id an agent step submits under: stable per run, step and attempt."""
    base = f"{workflow_run_id}-{task_id}"
    return base if attempt <= 1 else f"{base}-{attempt}"


async def post_inline_agent_request(
    pool: "Pool",
    *,
    agent_id: str,
    message: str,
    structured_output: dict[str, Any] | None,
    workflow_id: str,
    workflow_run_id: str,
    task_id: str,
    session_id: str,
    user_id: str,
    host_snapshot: dict[str, Any] | None = None,
    attempt: int = 1,
) -> str:
    """Submit one agent request on behalf of a workflow step and return its id.

    The request acts for the run's ``user_id``. Pair with
    ``collect_terminal_payload`` to await the result.
    """
    normalized = (
        normalize_structured_output_schema(structured_output)
        if isinstance(structured_output, dict)
        else _WORKFLOW_TASK_STRUCTURED_OUTPUT
    )
    return await pool.get_client(agent_id).post_request(
        message,
        session_id=session_id,
        user_id=user_id,
        structured_output=normalized,
        request_id=agent_step_request_id(workflow_run_id, task_id, attempt),
        host=host_snapshot,
        parent={
            "kind": "workflow",
            "workflow_id": workflow_id,
            "run_id": workflow_run_id,
            "task_id": task_id,
        },
    )


async def collect_terminal_payload(
    pool: "Pool", agent_id: str, request_id: str
) -> dict[str, Any]:
    """Follow the request's stream to its end; its completed payload, or raise."""
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


__all__ = [
    "agent_step_request_id",
    "collect_terminal_payload",
    "post_inline_agent_request",
]

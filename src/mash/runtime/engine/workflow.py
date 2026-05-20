"""DBOS workflow entrypoints for runtime request execution."""

from __future__ import annotations

from typing import Any

from dbos import DBOS

from ...logging import bound_request_id
from .. import context as context_helpers
from .steps import (
    commit_request_step,
    complete_request,
    fail_request,
    load_request_context,
    persist_completed_turn,
    plan_request_step,
    run_step_tool_call,
    start_request_trace,
)


def workflow_id_for(agent_id: str, request_id: str) -> str:
    return f"{agent_id}:{request_id}"


async def _run_tool_call_for_workflow(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    *,
    loop_index: int,
    call_index: int,
    tool_call: dict[str, Any],
) -> dict[str, Any]:
    if str(tool_call.get("name") or "") == "InvokeSubagent":
        # InvokeSubagent starts a child DBOS workflow. DBOS rejects that when the
        # call happens from step context, so keep this one tool invocation at
        # workflow scope while preserving the same result/event behavior.
        return await run_step_tool_call(
            agent_id,
            request_id,
            session_id,
            trace_id,
            workflow_state,
            tool_call,
        )
    return await DBOS.run_step_async(
        {"name": f"tool.call.{loop_index}.{call_index}"},
        run_step_tool_call,
        agent_id,
        request_id,
        session_id,
        trace_id,
        workflow_state,
        tool_call,
    )


async def execute_request_workflow(
    agent_id: str,
    request_id: str,
    message: str,
    session_id: str,
    request_metadata: dict[str, Any],
    *,
    require_runtime: Any,
) -> None:
    require_runtime(agent_id)
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    session_id = session_id.strip()
    if not session_id:
        raise ValueError("session_id is required")
    trace_id: str | None = None
    with bound_request_id(request_id):
        try:
            trace_id = await DBOS.run_step_async(
                {"name": "request.start"},
                start_request_trace,
                agent_id,
                request_id,
                session_id,
                message,
            )
            workflow_state = await DBOS.run_step_async(
                {"name": "context.load"},
                load_request_context,
                agent_id,
                request_id,
                session_id,
                trace_id,
                message,
            )
            while True:
                loop_index = int(workflow_state.get("loop_index") or 0)
                workflow_state = await DBOS.run_step_async(
                    {"name": f"step.plan.{loop_index}"},
                    plan_request_step,
                    agent_id,
                    request_id,
                    session_id,
                    trace_id,
                    workflow_state,
                )
                action_payload = dict(workflow_state.get("action") or {})
                tool_calls = context_helpers.tool_calls_from_action_payload(action_payload)

                for call_index, tool_call in enumerate(tool_calls):
                    existing_results = list(workflow_state.get("result_payloads") or [])
                    if call_index < len(existing_results):
                        continue
                    workflow_state = await _run_tool_call_for_workflow(
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        workflow_state,
                        loop_index=loop_index,
                        call_index=call_index,
                        tool_call={
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": dict(tool_call.arguments or {}),
                        },
                    )

                workflow_state = await DBOS.run_step_async(
                    {"name": f"step.commit.{loop_index}"},
                    commit_request_step,
                    agent_id,
                    request_id,
                    session_id,
                    trace_id,
                    workflow_state,
                )

                if bool(workflow_state.get("done")):
                    turn_payload = await DBOS.run_step_async(
                        {"name": "turn.persist"},
                        persist_completed_turn,
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        message,
                        workflow_state,
                        request_metadata,
                    )
                    await DBOS.run_step_async(
                        {"name": "request.complete"},
                        complete_request,
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        turn_payload,
                    )
                    return
        except Exception as exc:
            error_payload = {
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }
            await DBOS.run_step_async(
                {"name": "request.fail"},
                fail_request,
                agent_id,
                request_id,
                session_id,
                trace_id,
                error_payload,
            )
            raise RuntimeError(str(exc)) from None

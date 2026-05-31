"""DBOS workflow entrypoints for runtime request execution."""

from __future__ import annotations

import uuid
from typing import Any

from dbos import DBOS

from ...logging import bound_request_id
from .. import context as context_helpers
from .steps import (
    commit_request_step,
    complete_request,
    emit_interaction_ack,
    emit_interaction_create,
    fail_request,
    finalize_structured_output,
    load_request_context,
    persist_completed_turn,
    plan_request_step,
    run_step_tool_call,
    start_request_trace,
)


def workflow_id_for(agent_id: str, request_id: str) -> str:
    return f"{agent_id}:{request_id}"


async def _handle_ask_user_interaction(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    *,
    tool_call: dict[str, Any],
    loop_index: int,
) -> dict[str, Any]:
    """Intercept AskUser tool call and translate to a durable interaction."""
    from ...tools.ask_user import ASK_USER_DEFAULT_TIMEOUT_SECONDS

    arguments = dict(tool_call.get("arguments") or {})
    question = str(arguments.get("question") or "")
    options = arguments.get("options")
    interaction_type = "choice" if options else "info"
    timeout_seconds = ASK_USER_DEFAULT_TIMEOUT_SECONDS
    interaction_id = f"itr_{uuid.uuid4().hex[:12]}"

    await DBOS.run_step_async(
        {"name": f"ask_user.create.{loop_index}"},
        emit_interaction_create,
        agent_id,
        request_id,
        session_id,
        trace_id,
        interaction_id=interaction_id,
        interaction_type=interaction_type,
        prompt=question,
        options=options,
        timeout_seconds=timeout_seconds,
    )

    response = await DBOS.recv_async(interaction_id, timeout_seconds=timeout_seconds)

    timed_out = response is None
    if timed_out:
        response = [] if interaction_type == "choice" else ""

    await DBOS.run_step_async(
        {"name": f"ask_user.ack.{loop_index}"},
        emit_interaction_ack,
        agent_id,
        request_id,
        session_id,
        trace_id,
        interaction_id=interaction_id,
        response=response,
        timed_out=timed_out,
    )

    if isinstance(response, list):
        content = ", ".join(str(item) for item in response)
    else:
        content = str(response)

    result_payload = {
        "tool_call_id": tool_call.get("id", ""),
        "tool_name": "AskUser",
        "duration_ms": 0,
        "result": {
            "content": content,
            "is_error": False,
            "metadata": {"interaction_id": interaction_id, "timed_out": timed_out},
        },
    }

    return {
        **workflow_state,
        "result_payloads": list(workflow_state.get("result_payloads") or [])
        + [result_payload],
    }


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
    if str(tool_call.get("name") or "") == "AskUser":
        return await _handle_ask_user_interaction(
            agent_id,
            request_id,
            session_id,
            trace_id,
            workflow_state,
            tool_call=tool_call,
            loop_index=loop_index,
        )
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

                interaction = action_payload.get("interaction")
                if isinstance(interaction, dict):
                    interaction_type = str(interaction.get("type") or "info")
                    prompt = str(interaction.get("prompt") or "")
                    options = interaction.get("options")
                    timeout_seconds = int(interaction.get("timeout_seconds") or 300)
                    interaction_id = f"itr_{uuid.uuid4().hex[:12]}"

                    await DBOS.run_step_async(
                        {"name": f"interaction.create.{loop_index}"},
                        emit_interaction_create,
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        interaction_id=interaction_id,
                        interaction_type=interaction_type,
                        prompt=prompt,
                        options=options,
                        timeout_seconds=timeout_seconds,
                    )

                    response = await DBOS.recv_async(
                        interaction_id, timeout_seconds=timeout_seconds
                    )

                    timed_out = response is None
                    if timed_out:
                        if interaction_type == "approval":
                            response = "deny"
                        elif interaction_type == "choice":
                            response = []
                        else:
                            response = ""

                    await DBOS.run_step_async(
                        {"name": f"interaction.ack.{loop_index}"},
                        emit_interaction_ack,
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        interaction_id=interaction_id,
                        response=response,
                        timed_out=timed_out,
                    )

                    workflow_state = {
                        **workflow_state,
                        "interaction_response": response,
                    }

                tool_calls = context_helpers.tool_calls_from_action_payload(
                    action_payload
                )

                approval_denied = (
                    isinstance(interaction, dict)
                    and interaction.get("type") == "approval"
                    and workflow_state.get("interaction_response") in ("deny", "skip")
                )

                for call_index, tool_call in enumerate(tool_calls):
                    existing_results = list(workflow_state.get("result_payloads") or [])
                    if call_index < len(existing_results):
                        continue
                    if approval_denied:
                        denied_result = {
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_call.name,
                            "duration_ms": 0,
                            "result": {
                                "content": f"Tool execution denied by user ({workflow_state.get('interaction_response')})",
                                "is_error": True,
                                "metadata": {"denied": True},
                            },
                        }
                        workflow_state = {
                            **workflow_state,
                            "result_payloads": list(
                                workflow_state.get("result_payloads") or []
                            )
                            + [denied_result],
                        }
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
                    structured_output_request = request_metadata.get(
                        "structured_output_request"
                    )
                    if isinstance(structured_output_request, dict):
                        workflow_state = await DBOS.run_step_async(
                            {"name": "structured_output.finalize"},
                            finalize_structured_output,
                            agent_id,
                            request_id,
                            session_id,
                            trace_id,
                            workflow_state,
                            structured_output_request,
                        )
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

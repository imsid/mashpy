"""DBOS workflow entrypoints for runtime request execution."""

from __future__ import annotations

import uuid
from typing import Any

from dbos import DBOS

from ...logging import (
    bound_host_id,
    bound_request_id,
    bound_request_metadata,
    bound_session_id,
)
from ...logging.trace_context import bound_workflow_ids
from .. import context as context_helpers
from ..errors import classify_error, retry_transient
from ..requests import (
    caller_metadata_from_request_metadata,
    host_id_from_request_metadata,
)
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
    run_step_tool_batch,
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
    return await retry_transient(
        lambda: DBOS.run_step_async(
            {"name": f"tool.call.{loop_index}.{call_index}"},
            run_step_tool_call,
            agent_id,
            request_id,
            session_id,
            trace_id,
            workflow_state,
            tool_call,
        )
    )


def _raw_tool_call_is_parallel_safe(
    raw_tool_calls: list[Any], idx: int
) -> bool:
    """Read the parallel-safe flag the plan step stamped onto a tool call.

    Defaults to False (serial) when the flag is absent so older recorded
    actions keep today's one-step-per-call behavior.
    """
    raw = raw_tool_calls[idx] if 0 <= idx < len(raw_tool_calls) else None
    return bool(raw.get("parallel_safe")) if isinstance(raw, dict) else False


def _tool_call_exec_payload(tool_call: Any) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": dict(tool_call.arguments or {}),
    }


async def _run_tool_batch_for_workflow(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    *,
    loop_index: int,
    start_index: int,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run a contiguous run of parallel-safe calls as one atomic DBOS step."""
    return await retry_transient(
        lambda: DBOS.run_step_async(
            {"name": f"tool.batch.{loop_index}.{start_index}"},
            run_step_tool_batch,
            agent_id,
            request_id,
            session_id,
            trace_id,
            workflow_state,
            tool_calls,
        )
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
    with bound_request_id(request_id), bound_session_id(session_id), bound_host_id(
        host_id_from_request_metadata(request_metadata)
    ), bound_workflow_ids(
        request_metadata.get("workflow_id"),
        request_metadata.get("workflow_run_id"),
    ), bound_request_metadata(
        caller_metadata_from_request_metadata(request_metadata)
    ):
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
                request_metadata,
            )
            while True:
                loop_index = int(workflow_state.get("loop_index") or 0)
                workflow_state = await retry_transient(
                    lambda: DBOS.run_step_async(
                        {"name": f"step.plan.{loop_index}"},
                        plan_request_step,
                        agent_id,
                        request_id,
                        session_id,
                        trace_id,
                        workflow_state,
                    )
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
                raw_tool_calls = list(action_payload.get("tool_calls") or [])

                approval_denied = (
                    isinstance(interaction, dict)
                    and interaction.get("type") == "approval"
                    and workflow_state.get("interaction_response") in ("deny", "skip")
                )

                # Calls run in order. A maximal run of consecutive parallel-safe
                # calls executes concurrently as one atomic DBOS batch step; a
                # serial call (approval-gated, AskUser, InvokeSubagent,
                # parallel_safe=False) is a barrier that runs alone, so nothing
                # crosses it. `existing_results` lets a recovered workflow skip
                # calls whose results are already recorded.
                call_index = 0
                total_calls = len(tool_calls)
                while call_index < total_calls:
                    existing_results = list(workflow_state.get("result_payloads") or [])
                    if call_index < len(existing_results):
                        call_index += 1
                        continue
                    tool_call = tool_calls[call_index]
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
                            "result_payloads": existing_results + [denied_result],
                        }
                        call_index += 1
                        continue
                    if not _raw_tool_call_is_parallel_safe(
                        raw_tool_calls, call_index
                    ):
                        workflow_state = await _run_tool_call_for_workflow(
                            agent_id,
                            request_id,
                            session_id,
                            trace_id,
                            workflow_state,
                            loop_index=loop_index,
                            call_index=call_index,
                            tool_call=_tool_call_exec_payload(tool_call),
                        )
                        call_index += 1
                        continue
                    # Gather the maximal run of consecutive parallel-safe calls.
                    batch_end = call_index
                    while batch_end < total_calls and _raw_tool_call_is_parallel_safe(
                        raw_tool_calls, batch_end
                    ):
                        batch_end += 1
                    if batch_end - call_index == 1:
                        # A lone parallel-safe call: keep the single-step path so
                        # its event/step keys are unchanged.
                        workflow_state = await _run_tool_call_for_workflow(
                            agent_id,
                            request_id,
                            session_id,
                            trace_id,
                            workflow_state,
                            loop_index=loop_index,
                            call_index=call_index,
                            tool_call=_tool_call_exec_payload(tool_call),
                        )
                    else:
                        workflow_state = await _run_tool_batch_for_workflow(
                            agent_id,
                            request_id,
                            session_id,
                            trace_id,
                            workflow_state,
                            loop_index=loop_index,
                            start_index=call_index,
                            tool_calls=[
                                _tool_call_exec_payload(tool_calls[k])
                                for k in range(call_index, batch_end)
                            ],
                        )
                    call_index = batch_end

                structured_output_request = request_metadata.get(
                    "structured_output_request"
                )
                workflow_state = await DBOS.run_step_async(
                    {"name": f"step.commit.{loop_index}"},
                    commit_request_step,
                    agent_id,
                    request_id,
                    session_id,
                    trace_id,
                    workflow_state,
                    structured_output_request,
                )

                if bool(workflow_state.get("done")):
                    if (
                        isinstance(structured_output_request, dict)
                        and workflow_state.get("structured_output") is None
                    ):
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
            error_payload = classify_error(exc)
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

"""DBOS runtime step implementations and workflow-state transitions."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from ...core.context import Context, Response, ToolCall
from ...core.context import ToolResult as ContextToolResult
from ...core.llm.types import LLMContentBlock, LLMMessage, LLMRequest
from ...logging.events import AgentTraceEvent
from .. import context as context_helpers
from .. import factory as factory_helpers
from ..events import RuntimeEvent, RuntimeEventType
from ..requests import append_runtime_event

if TYPE_CHECKING:
    from ..service import AgentRuntime


def _require_runtime(agent_id: str) -> "AgentRuntime":
    # Import is deferred here to avoid a circular import through engine.dbos.
    from .dbos import (
        require_runtime as resolve_runtime,  # pylint: disable=import-outside-toplevel
    )

    return resolve_runtime(agent_id)


def _merge_tool_usage(
    existing: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for source in (existing or {}, snapshot or {}):
        for name, entry in dict(source).items():
            if not isinstance(entry, dict):
                continue
            target = merged.setdefault(str(name), {"tokens": 0, "invocations": 0})
            target["tokens"] = max(target["tokens"], int(entry.get("tokens", 0) or 0))
            target["invocations"] = max(
                target["invocations"],
                int(entry.get("invocations", 0) or 0),
            )
    return merged


def _record_tool_invocation(
    tool_usage: dict[str, Any] | None,
    tool_name: str,
) -> dict[str, dict[str, int]]:
    merged = _merge_tool_usage(tool_usage, None)
    cleaned_name = str(tool_name or "").strip()
    if not cleaned_name:
        return merged
    entry = merged.setdefault(cleaned_name, {"tokens": 0, "invocations": 0})
    entry["invocations"] += 1
    return merged


def _step_tool_result_payload(
    tool_call: ToolCall,
    result: ContextToolResult,
    *,
    duration_ms: int,
) -> dict[str, Any]:
    result_metadata = dict(result.metadata or {})
    payload: dict[str, Any] = {
        "tool_call_id": tool_call.id,
        "tool_name": tool_call.name,
        "duration_ms": int(duration_ms),
        "result": {
            "content": result.content,
            "is_error": result.is_error,
            "metadata": result_metadata,
        },
    }
    if isinstance(result.structured_output, dict):
        payload["structured_output"] = dict(result.structured_output)
    return payload


def _extract_tool_structured_output(
    result_payloads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Collect tool-asserted structured_output from a batch of result payloads.

    Returns None when no tool set it, the single value when exactly one did,
    and raises RuntimeError when more than one tool in the same batch set it.
    """
    found: list[dict[str, Any]] = [
        dict(rp["structured_output"])
        for rp in result_payloads
        if isinstance(rp.get("structured_output"), dict)
    ]
    if not found:
        return None
    if len(found) > 1:
        raise RuntimeError(
            f"multiple tools in the same turn set structured_output "
            f"({len(found)} found); exactly one tool per finishing turn may assert it"
        )
    return found[0]


def _validate_tool_structured_output(
    value: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    """Check that a tool-asserted structured_output dict satisfies the schema's required fields."""
    if not isinstance(value, dict):
        raise ValueError("tool structured_output must be a JSON object (dict)")
    required = schema.get("required") or []
    missing = [str(k) for k in required if k not in value]
    if missing:
        raise ValueError(
            f"tool structured_output is missing required field(s): {', '.join(missing)}"
        )


def _step_completed_payload(
    action_payload: dict[str, Any],
    *,
    duration_ms: int,
) -> dict[str, Any]:
    tool_calls = []
    for item in list(action_payload.get("tool_calls") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            tool_calls.append(name)
    return {
        "action_type": action_payload.get("action_type"),
        "tool_calls": tool_calls,
        "duration_ms": int(duration_ms),
    }


def _structured_output_instruction() -> str:
    return (
        "Produce the requested structured output for the preceding completed "
        "assistant response. Preserve the answer's facts and do not add new "
        "information."
    )


async def _plan_step_payload(
    runtime: "AgentRuntime",
    *,
    context: Context,
    session_id: str,
    trace_id: str,
    host: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agent = runtime.build_turn_agent(
        session_id=session_id, trace_id=trace_id, host=host
    )
    try:
        plan = await agent.plan_step(context)
        action = plan.action
        return {
            "action_type": action.type.value,
            "assistant_text": action.metadata.get("assistant_text"),
            "assistant_blocks": list(action.metadata.get("assistant_blocks") or []),
            "stop_reason": action.metadata.get("stop_reason"),
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": dict(tool_call.arguments or {}),
                    "parallel_safe": agent._is_parallel_safe(tool_call),
                }
                for tool_call in action.tool_calls
            ],
            "token_usage": dict(plan.token_usage or {}),
            "tool_usage": dict(plan.tool_usage or {}),
            "duration_ms": int(plan.duration_ms),
            "trace_id": plan.trace_id or trace_id,
            "context": context_helpers.serialize_context(context),
        }
    finally:
        await agent.tools.shutdown()


async def _run_tool_call_payload(
    runtime: "AgentRuntime",
    *,
    tool_call: ToolCall,
    session_id: str,
    trace_id: str,
    host: dict[str, Any] | None = None,
) -> tuple[ContextToolResult, int]:
    agent = runtime.build_turn_agent(
        session_id=session_id, trace_id=trace_id, host=host
    )
    started_at = time.time()
    try:
        result = await agent.execute_step_tool_call(tool_call)
        return result, int((time.time() - started_at) * 1000)
    finally:
        await agent.tools.shutdown()


async def _commit_step_payload(
    runtime: "AgentRuntime",
    *,
    context: Context,
    action_payload: dict[str, Any],
    result_payloads: list[dict[str, Any]],
    session_id: str,
    trace_id: str,
    step_index: int,
    tool_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    action = context_helpers.action_from_payload(action_payload)
    results = context_helpers.result_payloads_to_context_results(result_payloads)
    runtime.configure_turn_context(
        runtime.agent, session_id=session_id, trace_id=trace_id,
    )
    commit = runtime.agent.commit_step(
        context,
        action,
        results,
        step_index=step_index,
        tool_usage=tool_usage,
    )
    return {
        "context": context_helpers.serialize_context(commit.context),
        "done": bool(commit.done),
        "signals": dict(commit.signals or {}),
    }


async def _persist_turn_payload(
    runtime: "AgentRuntime",
    *,
    message: str,
    session_id: str,
    response: Response,
    signals: dict[str, Any],
    compaction_payload: dict[str, Any],
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response_metadata = dict(response.metadata or {})
    token_usage = response_metadata.get("token_usage") or {}
    response_metadata["token_usage"] = token_usage
    response_metadata["compaction_summary_text"] = compaction_payload.get(
        "compaction_summary_text"
    )
    response_metadata["compaction_summary_trace_id"] = compaction_payload.get(
        "compaction_summary_trace_id"
    )
    if extra_metadata:
        response_metadata.update(dict(extra_metadata))
    total_tokens = context_helpers.compute_turn_tokens(response_metadata)
    session_total_tokens = await context_helpers.get_session_total_tokens(
        runtime, session_id
    )
    session_total_tokens += total_tokens
    trace_id = response_metadata.get("trace_id")
    resolved_trace_id = str(trace_id or uuid.uuid4())
    # Workflow task and subagent turns (their request_metadata carries
    # workflow_id/run_id/task_id or subagent_id) share the session but are kept
    # out of the model's replayed conversation history.
    workflow_id = response_metadata.get("workflow_id") or None
    workflow_run_id = response_metadata.get("workflow_run_id") or None
    task_id = response_metadata.get("task_id") or None
    is_subagent = bool(response_metadata.get("subagent_id"))
    await runtime.store.save_turn(
        trace_id=resolved_trace_id,
        session_id=session_id,
        app_id=runtime.app_id,
        user_message=message,
        agent_response=response.text,
        signals=signals,
        session_total_tokens=session_total_tokens,
        metadata=response_metadata,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        task_id=task_id,
        replayable=workflow_id is None and not is_subagent,
    )
    response_payload = {
        "text": response.text,
        "signals": dict(signals or {}),
        "metadata": dict(response_metadata or {}),
    }
    if response.assistant_blocks:
        response_payload["assistant_blocks"] = list(response.assistant_blocks)
    if "structured_output" in response_metadata:
        response_payload["structured_output"] = response_metadata["structured_output"]
    return {
        "trace_id": resolved_trace_id,
        "response": response_payload,
        "session_total_tokens": session_total_tokens,
        "signals": dict(signals or {}),
        "response_metadata": dict(response_metadata or {}),
        "compaction_summary_text": compaction_payload.get("compaction_summary_text"),
        "compaction_summary_trace_id": compaction_payload.get(
            "compaction_summary_trace_id"
        ),
    }


async def start_request_trace(
    agent_id: str,
    request_id: str,
    session_id: str,
    message: str,
) -> str:
    runtime = _require_runtime(agent_id)
    trace_id = str(uuid.uuid4())
    # workflow_id / workflow_run_id are stamped by append_runtime_event from the
    # bound workflow context when the request was issued by a workflow task.
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.TRACE_STARTED.value,
            dedupe_key="request.started",
            payload={"message": message},
        ),
    )
    await runtime.event_logger.emit(
        AgentTraceEvent(
            event_type="agent.run.start",
            app_id=runtime.app_id,
            session_id=session_id,
            trace_id=trace_id,
            step_id=0,
            payload={"user_message": message},
        )
    )
    return trace_id


async def load_request_context(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    message: str,
    request_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = _require_runtime(agent_id)
    # The host snapshot is captured in this persisted step output, so recovery
    # replays the original composition even if the host is redefined while the
    # request is in flight.
    host = dict(request_metadata or {}).get("host") or None
    context_payload = await context_helpers.build_context_payload(
        runtime,
        session_id=session_id,
        message=message,
        system_prompt=factory_helpers.resolve_host_system_prompt(runtime, host),
    )
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.CONTEXT_LOADED.value,
            dedupe_key="context.loaded",
            payload={"message": message},
        ),
    )
    return {
        "context": dict(context_payload.get("context") or {}),
        "compaction": dict(context_payload.get("compaction") or {}),
        "host": host,
        "loop_index": 0,
        "aggregate_usage": {"input": 0, "output": 0},
        "tool_usage": {},
        "action": None,
        "result_payloads": [],
        "signals": {},
        "done": False,
    }


async def plan_request_step(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
) -> dict[str, Any]:
    runtime = _require_runtime(agent_id)
    loop_index = int(workflow_state.get("loop_index") or 0)
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            loop_index=loop_index,
            step_key=f"llm.think.started.{loop_index}",
            event_type=RuntimeEventType.LLM_THINK_STARTED.value,
            dedupe_key=f"llm.think.started.{loop_index}",
            payload={"loop_index": loop_index},
        ),
    )
    context = context_helpers.deserialize_context(
        runtime, workflow_state.get("context") or {}
    )
    plan_payload = await _plan_step_payload(
        runtime,
        context=context,
        session_id=session_id,
        trace_id=trace_id,
        host=workflow_state.get("host") or None,
    )
    action_payload = {
        key: value
        for key, value in dict(plan_payload or {}).items()
        if key != "context"
    }
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            loop_index=loop_index,
            step_key=f"llm.think.{loop_index}",
            event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
            dedupe_key=f"llm.think.{loop_index}",
            payload=action_payload,
        ),
    )
    tool_call_names = [
        str(tc.get("name") or "")
        for tc in list(action_payload.get("tool_calls") or [])
        if isinstance(tc, dict)
    ]
    needs_approval = runtime.agent.tools.tools_requiring_approval(tool_call_names)
    if needs_approval:
        action_payload["interaction"] = {
            "type": "approval",
            "prompt": f"Approve execution of: {', '.join(needs_approval)}?",
            "timeout_seconds": 300,
        }

    aggregate_usage = dict(workflow_state.get("aggregate_usage") or {})
    token_usage = dict(plan_payload.get("token_usage") or {})
    aggregate_usage["input"] = int(aggregate_usage.get("input", 0) or 0) + int(
        token_usage.get("input", 0) or 0
    )
    aggregate_usage["output"] = int(aggregate_usage.get("output", 0) or 0) + int(
        token_usage.get("output", 0) or 0
    )
    return {
        **dict(workflow_state or {}),
        "context": dict(plan_payload.get("context") or {}),
        "aggregate_usage": aggregate_usage,
        "tool_usage": _merge_tool_usage(
            workflow_state.get("tool_usage"),
            plan_payload.get("tool_usage"),
        ),
        "action": action_payload,
        "result_payloads": [],
        "signals": {},
        "done": False,
        "step_started_at": float(time.time()),
    }


async def _execute_and_record_tool_call(
    runtime: "AgentRuntime",
    *,
    request_id: str,
    session_id: str,
    trace_id: str,
    loop_index: int,
    tool_call: ToolCall,
    host: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run one tool call, emitting its started/completed events.

    Returns the result payload. This is the shared body for both the single
    serial step and the parallel batch step, so event keys and result shape are
    identical no matter which path executed the call.
    """
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            loop_index=loop_index,
            step_key=f"tool.call.started.{loop_index}.{tool_call.id}",
            event_type=RuntimeEventType.TOOL_CALL_STARTED.value,
            dedupe_key=f"tool.call.started.{loop_index}.{tool_call.id}",
            payload={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
            },
        ),
    )
    result, duration_ms = await _run_tool_call_payload(
        runtime,
        tool_call=tool_call,
        session_id=session_id,
        trace_id=trace_id,
        host=host,
    )
    result_payload = _step_tool_result_payload(
        tool_call,
        result,
        duration_ms=duration_ms,
    )
    event_type = (
        RuntimeEventType.SUBAGENT_CALL_COMPLETED.value
        if tool_call.name == "InvokeSubagent"
        else RuntimeEventType.TOOL_CALL_COMPLETED.value
    )
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            loop_index=loop_index,
            step_key=f"tool.call.{loop_index}.{tool_call.id}",
            event_type=event_type,
            dedupe_key=f"tool.call.{loop_index}.{tool_call.id}",
            payload=result_payload,
        ),
    )
    return result_payload


def _tool_call_from_payload(tool_call_payload: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(tool_call_payload.get("id") or ""),
        name=str(tool_call_payload.get("name") or ""),
        arguments=dict(tool_call_payload.get("arguments") or {}),
    )


async def run_step_tool_call(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    tool_call_payload: dict[str, Any],
) -> dict[str, Any]:
    runtime = _require_runtime(agent_id)
    loop_index = int(workflow_state.get("loop_index") or 0)
    tool_call = _tool_call_from_payload(tool_call_payload)
    result_payload = await _execute_and_record_tool_call(
        runtime,
        request_id=request_id,
        session_id=session_id,
        trace_id=trace_id,
        loop_index=loop_index,
        tool_call=tool_call,
        host=workflow_state.get("host") or None,
    )
    return {
        **dict(workflow_state or {}),
        "result_payloads": list(workflow_state.get("result_payloads") or [])
        + [result_payload],
        "tool_usage": _record_tool_invocation(
            workflow_state.get("tool_usage"),
            tool_call.name,
        ),
    }


async def run_step_tool_batch(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    tool_call_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute a batch of parallel-safe tool calls concurrently.

    Runs as a single DBOS step so crash recovery replays the batch atomically:
    either all of its results are recorded or the whole batch re-runs. Each call
    still builds its own turn agent (mirroring the serial path), so concurrent
    executions share no mutable agent state. Concurrency is capped by the
    agent's ``max_parallel_tools`` so a wide fan-out can't exhaust the
    connection pool or downstream limits. Results are appended in call order so
    they map back to their ``tool_call_id``.
    """
    runtime = _require_runtime(agent_id)
    loop_index = int(workflow_state.get("loop_index") or 0)
    host = workflow_state.get("host") or None
    tool_calls = [_tool_call_from_payload(p) for p in tool_call_payloads]

    max_parallel = max(1, int(runtime.agent.config.max_parallel_tools))
    semaphore = asyncio.Semaphore(max_parallel)

    async def _run(tool_call: ToolCall) -> dict[str, Any]:
        async with semaphore:
            return await _execute_and_record_tool_call(
                runtime,
                request_id=request_id,
                session_id=session_id,
                trace_id=trace_id,
                loop_index=loop_index,
                tool_call=tool_call,
                host=host,
            )

    result_payloads = await asyncio.gather(
        *(_run(tool_call) for tool_call in tool_calls)
    )

    tool_usage = workflow_state.get("tool_usage")
    for tool_call in tool_calls:
        tool_usage = _record_tool_invocation(tool_usage, tool_call.name)

    return {
        **dict(workflow_state or {}),
        "result_payloads": list(workflow_state.get("result_payloads") or [])
        + list(result_payloads),
        "tool_usage": tool_usage,
    }


async def commit_request_step(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    structured_output_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = _require_runtime(agent_id)
    loop_index = int(workflow_state.get("loop_index") or 0)
    context = context_helpers.deserialize_context(
        runtime, workflow_state.get("context") or {}
    )
    action_payload = dict(workflow_state.get("action") or {})
    result_payloads = list(workflow_state.get("result_payloads") or [])
    commit_payload = await _commit_step_payload(
        runtime,
        context=context,
        action_payload=action_payload,
        result_payloads=result_payloads,
        session_id=session_id,
        trace_id=trace_id,
        step_index=loop_index,
        tool_usage=dict(workflow_state.get("tool_usage") or {}),
    )
    done = bool(commit_payload.get("done"))
    next_loop_index = loop_index if done else loop_index + 1
    step_started_at = workflow_state.get("step_started_at")
    if step_started_at is None:
        step_duration_ms = 0
    else:
        try:
            step_duration_ms = max(
                0, int((time.time() - float(step_started_at)) * 1000)
            )
        except (TypeError, ValueError):
            step_duration_ms = 0
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            loop_index=loop_index,
            step_key=f"step.completed.{loop_index}",
            event_type=RuntimeEventType.STEP_COMPLETED.value,
            dedupe_key=f"step.completed.{loop_index}",
            payload=_step_completed_payload(
                action_payload,
                duration_ms=step_duration_ms,
            ),
        ),
    )
    updated: dict[str, Any] = {
        **dict(workflow_state or {}),
        "context": dict(commit_payload.get("context") or {}),
        "loop_index": next_loop_index,
        "signals": dict(commit_payload.get("signals") or {}),
        "action": None,
        "result_payloads": [],
        "done": done,
        "step_started_at": None,
    }
    if done and isinstance(structured_output_request, dict):
        tool_so = _extract_tool_structured_output(result_payloads)
        if tool_so is not None:
            _validate_tool_structured_output(tool_so, structured_output_request)
            updated["structured_output"] = tool_so
    return updated


async def persist_completed_turn(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    message: str,
    workflow_state: dict[str, Any],
    request_metadata: dict[str, Any],
) -> dict[str, Any]:
    runtime = _require_runtime(agent_id)
    response = context_helpers.response_from_context_payload(
        runtime,
        {
            "context": dict(workflow_state.get("context") or {}),
            "compaction": dict(workflow_state.get("compaction") or {}),
        },
    )
    response.metadata["trace_id"] = trace_id
    response.metadata["token_usage"] = dict(workflow_state.get("aggregate_usage") or {})
    structured_output = workflow_state.get("structured_output")
    if structured_output is not None:
        response.metadata["structured_output"] = structured_output
    turn_payload = await _persist_turn_payload(
        runtime,
        message=message,
        session_id=session_id,
        response=response,
        signals=dict(workflow_state.get("signals") or {}),
        compaction_payload=dict(workflow_state.get("compaction") or {}),
        extra_metadata=request_metadata,
    )
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.TURN_PERSISTED.value,
            dedupe_key="turn.persisted",
            payload=turn_payload,
        ),
    )
    return turn_payload


async def finalize_structured_output(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    workflow_state: dict[str, Any],
    structured_output_request: dict[str, Any],
) -> dict[str, Any]:
    del request_id
    runtime = _require_runtime(agent_id)
    context = context_helpers.deserialize_context(
        runtime,
        workflow_state.get("context") or {},
    )
    messages = context.get_messages_for_llm()
    messages.append(
        LLMMessage(
            role="user",
            content=[LLMContentBlock.text(_structured_output_instruction())],
        )
    )
    llm_request = LLMRequest(
        model=runtime.agent.llm.model,
        system=context.system_prompt,
        messages=messages,
        tools=[],
        max_tokens=runtime.agent.config.max_tokens,
        temperature=runtime.agent.config.temperature,
        use_prompt_caching=False,
        provider_options={"structured_output": dict(structured_output_request)},
    )
    llm = runtime._shared_llm
    llm.set_event_logger(runtime.event_logger, session_id, runtime.app_id)
    llm.set_trace_id(trace_id)
    response = await llm.send(llm_request)
    if response.stop_reason in {"max_tokens", "refusal", "error"}:
        raise RuntimeError(
            f"structured output finalizer failed with stop reason: {response.stop_reason}"
        )
    try:
        decoded = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"structured output finalizer returned invalid JSON: {exc.msg}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("structured output finalizer must return a JSON object")

    aggregate_usage = dict(workflow_state.get("aggregate_usage") or {})
    if response.usage is not None:
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        if input_tokens is not None:
            aggregate_usage["input"] = int(aggregate_usage.get("input", 0) or 0) + int(
                input_tokens
            )
        if output_tokens is not None:
            aggregate_usage["output"] = int(aggregate_usage.get("output", 0) or 0) + int(
                output_tokens
            )

    return {
        **dict(workflow_state or {}),
        "aggregate_usage": aggregate_usage,
        "structured_output": dict(decoded),
    }


INTERACTION_SCHEMAS = {
    "approval": {"type": "enum", "options": ["approve", "deny", "skip"]},
    "info": {"type": "text"},
    "choice": lambda options: {"type": "multi_select", "options": list(options)},
}


async def emit_interaction_create(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    *,
    interaction_id: str,
    interaction_type: str,
    prompt: str,
    options: list[str] | None = None,
    timeout_seconds: int = 300,
) -> None:
    runtime = _require_runtime(agent_id)
    if interaction_type == "choice":
        schema = INTERACTION_SCHEMAS["choice"](options or [])
    else:
        schema = INTERACTION_SCHEMAS.get(interaction_type, {"type": "text"})
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.INTERACTION_CREATE.value,
            dedupe_key=f"interaction.create.{interaction_id}",
            payload={
                "interaction_id": interaction_id,
                "type": interaction_type,
                "prompt": prompt,
                "schema": schema,
                "timeout_seconds": timeout_seconds,
            },
        ),
    )


async def emit_interaction_ack(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    *,
    interaction_id: str,
    response: Any,
    timed_out: bool = False,
) -> None:
    runtime = _require_runtime(agent_id)
    payload: dict[str, Any] = {
        "interaction_id": interaction_id,
        "response": response,
    }
    if timed_out:
        payload["timed_out"] = True
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.INTERACTION_ACK.value,
            dedupe_key=f"interaction.ack.{interaction_id}",
            payload=payload,
        ),
    )


async def complete_request(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str,
    turn_payload: dict[str, Any],
) -> None:
    runtime = _require_runtime(agent_id)
    response_payload = dict(turn_payload.get("response") or {})
    assistant_response = str(response_payload.get("text") or "")
    await runtime.event_logger.emit(
        AgentTraceEvent(
            event_type="agent.run.complete",
            app_id=runtime.app_id,
            session_id=session_id,
            trace_id=trace_id,
            payload={"assistant_response": assistant_response},
        )
    )
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.REQUEST_COMPLETED.value,
            dedupe_key="request.completed",
            payload={
                "request_id": request_id,
                "agent_id": runtime.app_id,
                "session_id": session_id,
                "status": "completed",
                "trace_id": trace_id,
                **dict(turn_payload or {}),
            },
        ),
    )


async def fail_request(
    agent_id: str,
    request_id: str,
    session_id: str,
    trace_id: str | None,
    error_payload: dict[str, Any],
) -> None:
    runtime = _require_runtime(agent_id)
    await append_runtime_event(
        runtime,
        RuntimeEvent(
            request_id=request_id,
            app_id=runtime.app_id,
            agent_id=runtime.app_id,
            trace_id=trace_id,
            session_id=session_id,
            event_type=RuntimeEventType.REQUEST_FAILED.value,
            dedupe_key="request.failed",
            payload={
                "request_id": request_id,
                "agent_id": runtime.app_id,
                "session_id": session_id,
                "status": "error",
                **dict(error_payload or {}),
            },
        ),
    )

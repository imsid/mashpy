"""A request's loop state, rebuilt from its events.

The event log is the only record of a running request. The loop keeps its
state in memory and writes each step's output as events with dedupe keys;
a request recovered after a crash rebuilds the state from those events:

- ``mash.context_load.end``: the system prompt, and the board position read
  (a request the companion routed) or the replayed turns
- ``chat.start`` / ``chat.end``: each model call and its assistant message
- ``execute_tool.end``: each tool result, the loop's own (AskUser, browser
  handoffs, denied calls) included
- ``mash.step.end``: each committed step

The rebuild applies them with the same functions the running loop uses
(``apply_plan``, ``commit_state``), so it arrives at the state the loop held.
A step with no ``chat.end`` leaves the state before its model call; a call
with no ``execute_tool.end`` is left out of the results, and the loop runs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...otel import semconv
from .. import context as context_helpers
from ..events import RuntimeEvent, RuntimeEventType
from ..events.types import event_attributes
from .steps import (
    apply_plan,
    commit_state,
    initial_state,
    record_invocation,
    with_approval,
)

if TYPE_CHECKING:
    from ..service import AgentRuntime


class UnrecoverableRequestError(RuntimeError):
    """The request's events do not hold what its loop needs to continue."""


def _action_from_chat_end(payload: dict[str, Any]) -> dict[str, Any]:
    """The step's action as the loop held it; its usage is in the attributes."""
    action = {
        key: value
        for key, value in dict(payload or {}).items()
        if key not in {"status", "attributes"}
    }
    usage = semconv.token_usage(event_attributes(payload))
    action["token_usage"] = {"input": usage["input"], "output": usage["output"]}
    return action


def result_payload_from_tool_end(
    tool_call: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """The loop's result payload for a call, from its ``execute_tool.end``."""
    result_payload: dict[str, Any] = {
        "tool_call_id": str(tool_call.get("id") or ""),
        "tool_name": str(tool_call.get("name") or ""),
        "duration_ms": int(payload.get("duration_ms") or 0),
        "result": dict(payload.get("result") or {}),
    }
    if "structured_output" in payload:
        result_payload["structured_output"] = payload["structured_output"]
    return result_payload


async def rebuild_request_state(
    runtime: "AgentRuntime",
    *,
    session_id: str,
    message: str,
    request_metadata: dict[str, Any],
    events: list[RuntimeEvent],
) -> dict[str, Any] | None:
    """The loop state the request's events record, or None before its context load."""
    load_end = next(
        (e for e in events if e.event_type == RuntimeEventType.CONTEXT_LOAD_END.value),
        None,
    )
    if load_end is None:
        return None
    loaded = dict(load_end.payload or {})
    if "system_prompt" not in loaded:
        raise UnrecoverableRequestError(
            "the request's context load records no system prompt; it started "
            "before this version of Mash and cannot be continued"
        )
    board = loaded.get("board")
    if isinstance(board, dict):
        # The board as it was at the context load: entries added since do not
        # change a recovered request.
        context, _ = await context_helpers.build_board_context(
            runtime,
            session_id,
            message,
            system_prompt=loaded["system_prompt"],
            up_to=int(board.get("up_to") or 0),
        )
    else:
        context, _ = await context_helpers.build_context_with_history(
            runtime,
            session_id,
            message,
            system_prompt=loaded["system_prompt"],
            trace_ids=[str(trace_id) for trace_id in loaded.get("history") or []],
        )
    state = initial_state(
        context=context_helpers.serialize_context(context),
        compaction=dict(loaded.get("compaction") or {}),
        request_metadata=request_metadata,
    )

    chat_starts: dict[int, RuntimeEvent] = {}
    chat_ends: dict[int, RuntimeEvent] = {}
    tool_ends: dict[tuple[int, str], RuntimeEvent] = {}
    step_ends: set[int] = set()
    for event in events:
        if event.loop_index is None:
            continue
        loop_index = int(event.loop_index)
        if event.event_type == RuntimeEventType.CHAT_START.value:
            chat_starts.setdefault(loop_index, event)
        elif event.event_type == RuntimeEventType.CHAT_END.value:
            chat_ends.setdefault(loop_index, event)
        elif event.event_type == RuntimeEventType.EXECUTE_TOOL_END.value:
            call_id = str((event.step_key or "").split(".", 3)[-1])
            tool_ends.setdefault((loop_index, call_id), event)
        elif event.event_type == RuntimeEventType.STEP_END.value:
            step_ends.add(loop_index)

    structured_output_request = request_metadata.get("structured_output_request")
    loop_index = 0
    while True:
        chat_end = chat_ends.get(loop_index)
        if chat_end is None:
            return state
        action_payload = with_approval(runtime, _action_from_chat_end(chat_end.payload or {}))
        started = chat_starts.get(loop_index) or chat_end
        state = apply_plan(
            runtime, state, action_payload, step_started_at=float(started.created_at)
        )
        results: list[dict[str, Any]] = []
        for tool_call in list(action_payload.get("tool_calls") or []):
            if not isinstance(tool_call, dict):
                continue
            end = tool_ends.get((loop_index, str(tool_call.get("id") or "")))
            if end is None:
                continue
            result_payload = result_payload_from_tool_end(tool_call, end.payload or {})
            results.append(result_payload)
            state = record_invocation(state, result_payload)
        state = {**state, "result_payloads": results}
        if loop_index not in step_ends:
            return state
        state = commit_state(runtime, state, structured_output_request)
        if state.get("done"):
            return state
        loop_index += 1


__all__ = [
    "UnrecoverableRequestError",
    "rebuild_request_state",
    "result_payload_from_tool_end",
]

"""Runtime event-sourced workflow execution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...core.context import Action, ActionType, Response, ToolCall
from ...logging import AgentTraceEvent
from ..errors import classify_error
from .types import RuntimeEvent, RuntimeEventType, RuntimeReplayState

if TYPE_CHECKING:
    from ..runtime import MashAgentRuntime


@dataclass(frozen=True)
class _NextStep:
    event_type: RuntimeEventType
    loop_index: int | None = None
    step_key: str | None = None
    tool_call: ToolCall | None = None


class RuntimeRecoveryManager:
    """Resume incomplete runtime requests on startup."""

    def __init__(self, runtime: "MashAgentRuntime") -> None:
        self._runtime = runtime

    async def recover(self) -> None:
        request_ids = await self._runtime.runtime_store.list_incomplete_request_ids(
            app_id=self._runtime.app_id,
            agent_id=self._runtime.app_id,
        )
        for request_id in request_ids:
            self._runtime.schedule_request_execution(request_id)


class RuntimeWorkflowExecutor:
    """Advance one request by replaying persisted runtime events."""

    def __init__(self, runtime: "MashAgentRuntime") -> None:
        self._runtime = runtime

    async def load_state(self, request_id: str) -> RuntimeReplayState:
        events = await self._runtime.runtime_store.list_events(request_id, after_seq=0)
        if not events:
            raise KeyError(request_id)
        accepted = events[0]
        message = str(accepted.payload.get("message") or "")
        initial_session_id = accepted.payload.get("initial_session_id")
        if initial_session_id is not None:
            initial_session_id = str(initial_session_id)
        state = RuntimeReplayState(
            request_id=request_id,
            app_id=accepted.app_id,
            agent_id=accepted.agent_id,
            message=message,
            initial_session_id=initial_session_id,
            turn_metadata=dict(accepted.payload.get("turn_metadata") or {}),
        )
        for event in events:
            self._apply_event(state, event)
        return state

    async def run(self, request_id: str) -> None:
        while True:
            state = await self.load_state(request_id)
            if state.is_terminal:
                return
            next_step = self._determine_next_step(state)
            if next_step is None:
                return
            try:
                await self._perform_step(state, next_step)
            except Exception as exc:
                retryable = bool(classify_error(exc).get("retryable"))
                failure_payload = {
                    **classify_error(exc),
                    "step_kind": next_step.event_type.value,
                    "retryable": retryable,
                    "attempt": self._attempt_for_step(state, next_step.step_key),
                }
                await self._runtime.append_runtime_event(
                    RuntimeEvent(
                        request_id=state.request_id,
                        trace_id=state.trace_id,
                        app_id=self._runtime.app_id,
                        agent_id=self._runtime.app_id,
                        session_id=state.session_id,
                        event_type=RuntimeEventType.STEP_FAILED.value,
                        loop_index=next_step.loop_index,
                        step_key=next_step.step_key,
                        payload=failure_payload,
                    )
                )
                if not retryable or failure_payload["attempt"] >= 3:
                    await self._runtime.append_runtime_event(
                        RuntimeEvent(
                            request_id=state.request_id,
                            trace_id=state.trace_id,
                            app_id=self._runtime.app_id,
                            agent_id=self._runtime.app_id,
                            session_id=state.session_id,
                            event_type=RuntimeEventType.REQUEST_FAILED.value,
                            payload={
                                "request_id": state.request_id,
                                "agent_id": self._runtime.app_id,
                                "status": "error",
                                "session_id": state.session_id,
                                **classify_error(exc),
                            },
                        )
                    )
                    return
            else:
                continue

    def _attempt_for_step(self, state: RuntimeReplayState, step_key: str | None) -> int:
        if not step_key:
            return 1
        failures = state.step_failures.get(step_key, [])
        return len(failures) + 1

    async def _perform_step(self, state: RuntimeReplayState, step: _NextStep) -> None:
        if step.event_type == RuntimeEventType.TRACE_STARTED:
            trace_id = str(uuid.uuid4())
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.initial_session_id,
                    event_type=RuntimeEventType.TRACE_STARTED.value,
                    payload={"trace_id": trace_id},
                )
            )
            await self._runtime.event_logger.emit(
                AgentTraceEvent(
                    event_type="agent.run.start",
                    app_id=self._runtime.app_id,
                    session_id=state.initial_session_id,
                    trace_id=trace_id,
                    payload={"user_message": state.message},
                )
            )
            return

        if step.event_type == RuntimeEventType.SESSION_RESOLVED:
            session_id = (state.initial_session_id or self._runtime.default_session_id).strip()
            if not session_id:
                session_id = self._runtime.default_session_id
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=session_id,
                    event_type=RuntimeEventType.SESSION_RESOLVED.value,
                    step_key="session.resolve",
                    payload={"session_id": session_id},
                )
            )
            return

        if step.event_type == RuntimeEventType.CONTEXT_LOADED:
            payload = await self._runtime.build_context_payload(
                session_id=state.session_id or self._runtime.default_session_id,
                message=state.message,
            )
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.session_id,
                    event_type=RuntimeEventType.CONTEXT_LOADED.value,
                    step_key="context.load",
                    payload=payload,
                )
            )
            return

        if step.event_type == RuntimeEventType.LLM_THINK_COMPLETED:
            context = self._runtime.rebuild_context(state)
            action = await self._runtime.run_durable_think(
                context=context,
                session_id=state.session_id or self._runtime.default_session_id,
                trace_id=state.trace_id or str(uuid.uuid4()),
            )
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.session_id,
                    event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                    loop_index=step.loop_index,
                    step_key=step.step_key,
                    payload=action,
                )
            )
            return

        if step.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED,
        }:
            if step.tool_call is None:
                raise RuntimeError("tool_call is required")
            started_at = time.time()
            result = await self._runtime.run_durable_tool_call(
                tool_call=step.tool_call,
                session_id=state.session_id or self._runtime.default_session_id,
                trace_id=state.trace_id or str(uuid.uuid4()),
            )
            payload = {
                "tool_call_id": step.tool_call.id,
                "tool_name": step.tool_call.name,
                "result": {
                    "content": result.content,
                    "is_error": result.is_error,
                    "metadata": dict(result.metadata or {}),
                },
                "duration_ms": int((time.time() - started_at) * 1000),
            }
            if step.event_type == RuntimeEventType.SUBAGENT_CALL_COMPLETED:
                payload["subagent_id"] = str(step.tool_call.arguments.get("agent_id") or "")
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.session_id,
                    event_type=step.event_type.value,
                    loop_index=step.loop_index,
                    step_key=step.step_key,
                    payload=payload,
                )
            )
            return

        if step.event_type == RuntimeEventType.TURN_PERSISTED:
            context = self._runtime.rebuild_context(state)
            final_action = self._runtime.get_terminal_action(state)
            if final_action is None:
                raise RuntimeError("terminal action is required before persisting turn")
            if final_action.type == ActionType.FINISH:
                context.mark_complete()
            else:
                context.mark_complete()
            signals = self._runtime.collect_durable_signals(
                context,
                final_action,
                self._runtime.replay_all_tool_results(state),
            )
            context.signals.update(signals)
            response = Response.from_context(context)
            response.metadata["trace_id"] = state.trace_id
            response.metadata["token_usage"] = dict(
                self._runtime.get_replayed_token_usage(state)
            )
            result = await self._runtime.persist_durable_turn(
                message=state.message,
                session_id=state.session_id or self._runtime.default_session_id,
                response=response,
                signals=signals,
                compaction_payload=(
                    dict(state.context_payload.get("compaction") or {})
                    if state.context_payload
                    else {}
                ),
                extra_metadata=state.turn_metadata,
            )
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.session_id,
                    event_type=RuntimeEventType.TURN_PERSISTED.value,
                    step_key=step.step_key,
                    payload=result,
                )
            )
            return

        if step.event_type == RuntimeEventType.REQUEST_COMPLETED:
            context = self._runtime.rebuild_context(state)
            token_usage = self._runtime.get_replayed_token_usage(state)
            turn_payload = state.turn_persisted_payload or {}
            response_signals = dict(turn_payload.get("signals") or {})
            context.signals.update(response_signals)
            response = Response.from_context(context)
            await self._runtime.event_logger.emit(
                AgentTraceEvent(
                    event_type="agent.run.complete",
                    app_id=self._runtime.app_id,
                    session_id=state.session_id,
                    trace_id=state.trace_id,
                    payload={"assistant_response": response.text},
                )
            )
            await self._runtime.append_runtime_event(
                RuntimeEvent(
                    request_id=state.request_id,
                    trace_id=state.trace_id,
                    app_id=self._runtime.app_id,
                    agent_id=self._runtime.app_id,
                    session_id=state.session_id,
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    step_key=step.step_key,
                    payload={
                        "request_id": state.request_id,
                        "agent_id": self._runtime.app_id,
                        "status": "completed",
                        "session_id": state.session_id,
                        "response": {
                            "text": response.text,
                            "signals": response_signals,
                            "metadata": {
                                **dict(turn_payload.get("response_metadata") or {}),
                                **dict(response.metadata or {}),
                                "trace_id": state.trace_id,
                                "token_usage": token_usage,
                            },
                        },
                        "compaction_summary_text": turn_payload.get("compaction_summary_text"),
                        "compaction_summary_turn_id": turn_payload.get("compaction_summary_turn_id"),
                        "session_total_tokens": turn_payload.get("session_total_tokens", 0),
                    },
                )
            )
            return

        raise RuntimeError(f"unsupported runtime step: {step.event_type}")

    def _apply_event(self, state: RuntimeReplayState, event: RuntimeEvent) -> None:
        state.events.append(event)
        if event.event_type == RuntimeEventType.TRACE_STARTED.value:
            state.trace_id = str(event.payload.get("trace_id") or event.trace_id or "")
        elif event.event_type == RuntimeEventType.SESSION_RESOLVED.value:
            state.session_id = str(event.payload.get("session_id") or event.session_id or "")
        elif event.event_type == RuntimeEventType.CONTEXT_LOADED.value:
            state.context_payload = dict(event.payload or {})
        elif event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value and event.loop_index is not None:
            state.loop_actions[int(event.loop_index)] = dict(event.payload or {})
        elif event.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        } and event.loop_index is not None:
            state.loop_results.setdefault(int(event.loop_index), []).append(event)
        elif event.event_type == RuntimeEventType.TURN_PERSISTED.value:
            state.turn_persisted_payload = dict(event.payload or {})
        elif event.event_type == RuntimeEventType.REQUEST_COMPLETED.value:
            state.terminal_payload = dict(event.payload or {})
        elif event.event_type == RuntimeEventType.REQUEST_FAILED.value:
            state.failure_payload = dict(event.payload or {})
        elif event.event_type == RuntimeEventType.STEP_FAILED.value and event.step_key:
            state.step_failures.setdefault(event.step_key, []).append(event)

        if event.trace_id and not state.trace_id:
            state.trace_id = event.trace_id
        if event.session_id and not state.session_id:
            state.session_id = event.session_id

    def _determine_next_step(self, state: RuntimeReplayState) -> _NextStep | None:
        if state.is_terminal:
            return None
        if not state.trace_id:
            return _NextStep(RuntimeEventType.TRACE_STARTED)
        if not state.session_id:
            return _NextStep(RuntimeEventType.SESSION_RESOLVED, step_key="session.resolve")
        if state.context_payload is None:
            return _NextStep(RuntimeEventType.CONTEXT_LOADED, step_key="context.load")

        max_steps = self._runtime.get_max_steps()
        for loop_index in range(max_steps):
            action_payload = state.loop_actions.get(loop_index)
            if action_payload is None:
                step_key = f"llm.think:{loop_index}"
                if self._can_retry_step(state, step_key):
                    return _NextStep(
                        RuntimeEventType.LLM_THINK_COMPLETED,
                        loop_index=loop_index,
                        step_key=step_key,
                    )
                continue
            action = self._runtime.action_from_payload(action_payload)
            if action.type == ActionType.TOOL_CALL:
                completed = {str(event.payload.get("tool_call_id") or "") for event in state.loop_results.get(loop_index, [])}
                for tool_call in action.tool_calls:
                    if tool_call.id in completed:
                        continue
                    if tool_call.name == "InvokeSubagent":
                        event_type = RuntimeEventType.SUBAGENT_CALL_COMPLETED
                        prefix = "subagent.call"
                    else:
                        event_type = RuntimeEventType.TOOL_CALL_COMPLETED
                        prefix = "tool.call"
                    step_key = f"{prefix}:{loop_index}:{tool_call.id}"
                    if self._can_retry_step(state, step_key):
                        return _NextStep(
                            event_type,
                            loop_index=loop_index,
                            step_key=step_key,
                            tool_call=tool_call,
                        )
                continue

            if state.turn_persisted_payload is None:
                return _NextStep(RuntimeEventType.TURN_PERSISTED, step_key="turn.persist")
            return _NextStep(RuntimeEventType.REQUEST_COMPLETED, step_key="request.complete")

        if state.turn_persisted_payload is None:
            return _NextStep(RuntimeEventType.TURN_PERSISTED, step_key="turn.persist")
        return _NextStep(RuntimeEventType.REQUEST_COMPLETED, step_key="request.complete")

    def _can_retry_step(self, state: RuntimeReplayState, step_key: str) -> bool:
        failures = state.step_failures.get(step_key, [])
        return len(failures) < 3

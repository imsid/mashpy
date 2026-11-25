"""The request loop: one agent request, continued from its events.

The loop runs the same steps in the same order whether a request is new or
recovered after a crash. Its state is one dict the steps thread through each
other (context, loop index, the model's action, the tool results so far),
held in memory. The event log is the only record of it: each step appends its
output as events with dedupe keys, and a recovered request rebuilds the state
from them (``rebuild.py``).

Work with no end event runs again: a model call with no ``chat.end``, a tool
call with no ``execute_tool.end``. Its events are deduped by key, and an
interaction's id is derived from its place in the request, so a rerun finds
the interaction already open and its answer if it has one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...core.context import ToolCall
from ...logging import (
    bound_event_session_id,
    bound_host_id,
    bound_request_id,
    bound_request_metadata,
    bound_session_id,
    bound_subagent_id_prefix,
    bound_trace_id,
)
from ...logging.trace_context import bound_workflow_ids
from ...tools.forward import FORWARD_ANSWER
from ...tools.subagent import bound_interaction_relay
from .. import context as context_helpers
from ..errors import classify_error, retry_transient
from ..events import RuntimeEventType
from ..requests import (
    caller_metadata_from_request_metadata,
    host_id_from_request_metadata,
)
from .rebuild import rebuild_request_state
from .steps import (
    commit_request_step,
    complete_request,
    derive_interaction_id,
    emit_interaction_ack,
    emit_interaction_create,
    end_browser_handoff,
    fail_request,
    finalize_structured_output,
    interaction_schema,
    load_request_context,
    persist_completed_turn,
    persist_failed_turn,
    plan_request_step,
    record_tool_end,
    record_tool_start,
    run_step_tool_batch,
    run_step_tool_call,
    start_browser_handoff,
    start_request_trace,
)

if TYPE_CHECKING:
    from ..events import RuntimeEvent
    from ..service import AgentRuntime


log = logging.getLogger(__name__)


class RequestCancelled(Exception):
    """Raised at a step boundary once the request has been cancelled."""


_CANCELLED = object()


async def _resolved(value: Any) -> Any:
    return value


def _tool_call(payload: dict[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        arguments=dict(payload.get("arguments") or {}),
    )


def _interaction_timeout_default(interaction_type: str) -> Any:
    if interaction_type == "approval":
        return "deny"
    if interaction_type == "browser":
        return "cancel"
    if interaction_type == "choice":
        return []
    return ""


class RequestRun:
    """One request's loop in this process."""

    def __init__(
        self,
        runtime: "AgentRuntime",
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self.runtime = runtime
        self.request_id = request_id
        self.message = message
        self.session_id = session_id
        self.request_metadata = dict(request_metadata or {})
        # Read from the accepted request when the loop starts.
        self.trace_id: str | None = None
        self.cancelled = asyncio.Event()
        self._interaction_waiters: dict[str, asyncio.Event] = {}
        # The agent's running-request slots (MASH_AGENT_MAX_RUNNING). The loop
        # holds one while it works and gives it back while it waits on the
        # user or on its RunSubAgents children.
        self._slots = slots
        self._slot_held = False

    # --- running slots -------------------------------------------------------

    async def _take_slot(self) -> None:
        """Wait for a running slot; a cancel while waiting raises RequestCancelled."""
        if self._slots is None or self._slot_held:
            return
        acquire = asyncio.ensure_future(self._slots.acquire())
        cancelled = asyncio.ensure_future(self.cancelled.wait())
        try:
            await asyncio.wait({acquire, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancelled.cancel()
            if acquire.done() and not acquire.cancelled():
                self._slot_held = True
            else:
                acquire.cancel()
        if not self._slot_held:
            raise RequestCancelled(self.request_id)

    def _give_slot(self) -> None:
        if self._slots is not None and self._slot_held:
            self._slot_held = False
            self._slots.release()

    @contextlib.asynccontextmanager
    async def _slot_released(self) -> AsyncIterator[None]:
        """Give the slot back for a wait, and take one again after it."""
        self._give_slot()
        try:
            yield
        finally:
            await self._take_slot()

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise RequestCancelled(self.request_id)

    # --- cancel and interactions --------------------------------------------

    def cancel(self) -> None:
        self.cancelled.set()
        for waiter in self._interaction_waiters.values():
            waiter.set()

    def wake_interaction(self, interaction_id: str) -> None:
        waiter = self._interaction_waiters.get(interaction_id)
        if waiter is not None:
            waiter.set()

    async def _interaction_end(self, interaction_id: str) -> "RuntimeEvent | None":
        events = await self.runtime.runtime_store.list_request_events(self.request_id)
        return next(
            (
                event
                for event in events
                if event.event_type == RuntimeEventType.INTERACTION_END.value
                and (event.payload or {}).get("interaction_id") == interaction_id
            ),
            None,
        )

    async def _await_answer(
        self, interaction_id: str, *, deadline: float | None, interaction_type: str
    ) -> tuple[Any, bool]:
        """Wait for the interaction's ``interaction.end``; append a timeout at the deadline.

        Without a deadline it waits until an answer or a cancel.

        Returns ``(response, timed_out)``, or ``(_CANCELLED, False)`` when the
        interaction was cancelled.
        """
        waiter = asyncio.Event()
        self._interaction_waiters[interaction_id] = waiter
        try:
            while True:
                end = await self._interaction_end(interaction_id)
                if end is None and deadline is not None and time.time() >= deadline:
                    end = await emit_interaction_ack(
                        self.runtime,
                        self.request_id,
                        self.session_id,
                        self.trace_id,
                        interaction_id=interaction_id,
                        response=_interaction_timeout_default(interaction_type),
                        timed_out=True,
                    )
                if end is not None:
                    payload = end.payload or {}
                    if payload.get("cancelled"):
                        return _CANCELLED, False
                    return payload.get("response"), bool(payload.get("timed_out"))
                waiter.clear()
                timeout = None if deadline is None else max(0.0, deadline - time.time())
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._interaction_waiters.pop(interaction_id, None)

    async def _run_interaction(
        self,
        *,
        loop_index: int,
        interaction_type: str,
        prompt: str,
        options: list[str] | None,
        timeout_seconds: int | None,
        step_prefix: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, Any, bool]:
        """Open an interaction and wait for its answer, reopening after a cancel.

        A request cancelled while it waits here acks the interaction cancelled.
        A later resume finds that ack and opens a new interaction, numbered by
        ``attempt``. ``step_prefix`` keeps the interaction sites (the AskUser
        tool, the approval gate, a browser handoff) apart within one loop.
        """
        attempt = 0
        while True:
            step_name = f"{step_prefix}.open.{loop_index}.{attempt}"
            opened = await self._open_interaction(
                interaction_id=derive_interaction_id(f"{self.request_id}:{step_name}"),
                interaction_type=interaction_type,
                prompt=prompt,
                options=options,
                timeout_seconds=timeout_seconds,
                extra=extra,
            )
            interaction_id = str(opened["interaction_id"])
            async with self._slot_released():
                response, timed_out = await self._await_answer(
                    interaction_id,
                    deadline=None if opened["deadline"] is None else float(opened["deadline"]),
                    interaction_type=interaction_type,
                )
            if response is _CANCELLED:
                self._check_cancelled()
                attempt += 1
                continue
            return interaction_id, response, timed_out

    async def _open_interaction(
        self,
        *,
        interaction_id: str,
        interaction_type: str,
        prompt: str,
        options: list[str] | None,
        timeout_seconds: int | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Open the interaction, or find it open; its id and deadline.

        The id is derived from the interaction's place in the request, so a
        recovered request finds the one it opened. The deadline is the stored
        open time plus the stored timeout, so a recovery does not extend it.
        """
        # The policy caps every interaction's deadline; an interaction with no
        # timeout of its own (a browser handoff) takes the cap, and with no
        # cap waits until answered or cancelled.
        wait_max = (self.request_metadata.get("policy") or {}).get("wait_max_seconds")
        if wait_max is not None:
            timeout_seconds = (
                int(wait_max) if timeout_seconds is None else min(int(timeout_seconds), int(wait_max))
            )
        stored = await emit_interaction_create(
            self.runtime,
            self.request_id,
            self.session_id,
            self.trace_id,
            interaction_id=interaction_id,
            interaction_type=interaction_type,
            prompt=prompt,
            schema=interaction_schema(interaction_type, options),
            timeout_seconds=timeout_seconds,
            extra=extra,
        )
        stored_timeout = (stored.payload or {}).get("timeout_seconds")
        return {
            "interaction_id": interaction_id,
            "deadline": (
                None
                if stored_timeout is None
                else float(stored.created_at) + float(stored_timeout)
            ),
        }

    async def _await_close(self, interaction_id: str) -> dict[str, Any]:
        """Wait, without a deadline, for an interaction's ``interaction.end``.

        A relayed interaction has no deadline of its own: the child's closes it.
        """
        waiter = asyncio.Event()
        self._interaction_waiters[interaction_id] = waiter
        try:
            while True:
                # Cleared before the read, so an answer stored after the read
                # still wakes the wait.
                waiter.clear()
                end = await self._interaction_end(interaction_id)
                if end is not None:
                    return dict(end.payload or {})
                if self.cancelled.is_set():
                    return {"response": None, "cancelled": True}
                await waiter.wait()
        finally:
            self._interaction_waiters.pop(interaction_id, None)

    # --- tool calls ----------------------------------------------------------

    async def _ask_user(
        self,
        *,
        tool_call: dict[str, Any],
        loop_index: int,
        call_index: int,
    ) -> dict[str, Any]:
        """Turn an AskUser tool call into an interaction; the call's result payload."""
        from ...tools.ask_user import ASK_USER_DEFAULT_TIMEOUT_SECONDS

        arguments = dict(tool_call.get("arguments") or {})
        options = arguments.get("options")
        interaction_id, response, timed_out = await self._run_interaction(
            loop_index=loop_index,
            interaction_type="choice" if options else "info",
            prompt=str(arguments.get("question") or ""),
            options=options,
            timeout_seconds=ASK_USER_DEFAULT_TIMEOUT_SECONDS,
            step_prefix=f"ask_user.{call_index}",
        )
        if isinstance(response, list):
            content = ", ".join(str(item) for item in response)
        else:
            content = str(response)
        return {
            "tool_call_id": tool_call.get("id", ""),
            "tool_name": "AskUser",
            "duration_ms": 0,
            "result": {
                "content": content,
                "is_error": False,
                "metadata": {"interaction_id": interaction_id, "timed_out": timed_out},
            },
        }

    async def _browser_handoff(
        self,
        *,
        tool_call: dict[str, Any],
        loop_index: int,
        call_index: int,
    ) -> dict[str, Any]:
        """Turn browser_handoff into a ``browser`` interaction; the call's result payload.

        The interaction carries no link: one is made each time someone asks
        for it on the event, and lasts a few minutes, while the interaction
        may wait as long as the policy allows. Ending the handoff revokes
        every link and returns the page as the user left it. A recovered
        request that has not recorded the result yet brings the tab to the
        front again and finds its interaction open.
        """
        arguments = dict(tool_call.get("arguments") or {})
        reason = str(arguments.get("reason") or "The page needs you.").strip()
        started = await start_browser_handoff(self.runtime, self.request_id)
        metadata: dict[str, Any] = {}
        images: list[dict[str, Any]] = []
        if "error" in started:
            content, is_error = str(started["error"]), True
        else:
            try:
                interaction_id, response, timed_out = await self._run_interaction(
                    loop_index=loop_index,
                    interaction_type="browser",
                    prompt=reason,
                    options=None,
                    timeout_seconds=None,
                    step_prefix=f"browser.handoff.{call_index}",
                )
            except RequestCancelled:
                # Cancelled while it waited: the links go now, not at expiry.
                with contextlib.suppress(Exception):
                    await end_browser_handoff(self.runtime, self.request_id)
                raise
            page = await end_browser_handoff(self.runtime, self.request_id)
            metadata = {"interaction_id": interaction_id, "timed_out": timed_out}
            if response == "done" and not timed_out:
                content = (
                    "The user is done in the browser. The page now:\n"
                    f"{page.get('content') or ''}"
                )
                is_error = bool(page.get("is_error"))
                images = list(page.get("images") or [])
            elif timed_out:
                content = "The user did not finish in the browser before the wait ran out."
                is_error = True
            else:
                content, is_error = "The user cancelled the handoff.", True
        result: dict[str, Any] = {"content": content, "is_error": is_error, "metadata": metadata}
        if images:
            result["images"] = images
        return {
            "tool_call_id": tool_call.get("id", ""),
            "tool_name": "browser_handoff",
            "duration_ms": 0,
            "result": result,
        }

    async def _forward_answer(
        self, state: dict[str, Any], *, tool_call: dict[str, Any]
    ) -> dict[str, Any]:
        """Check a ForwardAnswer call; its result payload, with the reply when accepted.

        The child is looked up in the full ``RunSubAgents`` results this request
        recorded on ``execute_tool.end``, so an answer the context shows as a
        preview still forwards whole. A refusal is an error result, and the
        model goes on.
        """

        def refused(message: str) -> dict[str, Any]:
            return {
                "tool_call_id": tool_call.get("id", ""),
                "tool_name": FORWARD_ANSWER,
                "duration_ms": 0,
                "result": {"content": message, "is_error": True, "metadata": {}},
            }

        calls = list((state.get("action") or {}).get("tool_calls") or [])
        if len(calls) != 1:
            return refused(
                "ForwardAnswer must be the only tool call in its step; call it alone "
                "after RunSubAgents has returned."
            )
        request_id = str((tool_call.get("arguments") or {}).get("request_id") or "").strip()
        if not request_id:
            return refused("request_id is required")
        entry = None
        for event in await self.runtime.runtime_store.list_request_events(self.request_id):
            payload = event.payload or {}
            if (
                event.event_type != RuntimeEventType.EXECUTE_TOOL_END.value
                or (payload.get("attributes") or {}).get("gen_ai.tool.name") != "RunSubAgents"
            ):
                continue
            result = payload.get("result") or {}
            if result.get("is_error"):
                continue
            try:
                entries = json.loads(str(result.get("content") or ""))
            except ValueError:
                continue
            for candidate in entries if isinstance(entries, list) else []:
                if isinstance(candidate, dict) and candidate.get("request_id") == request_id:
                    entry = candidate
        if entry is None:
            return refused(
                f"no RunSubAgents entry with request_id '{request_id}' in this turn"
            )
        status = entry.get("status")
        if status != "completed":
            return refused(
                f"the child request is {status}, not completed: inspect it with "
                "InspectAgentTrace and report what happened instead of forwarding"
            )
        response = entry.get("response")
        if not isinstance(response, str) or not response.strip():
            return refused("the child has no text response to forward; write your own reply")
        return {
            "tool_call_id": tool_call.get("id", ""),
            "tool_name": FORWARD_ANSWER,
            "duration_ms": 0,
            "result": {
                "content": json.dumps({"forwarded": request_id}),
                "is_error": False,
                "metadata": {"forwarded": request_id},
            },
            "reply": response,
        }

    async def _loop_handled_call(
        self,
        state: dict[str, Any],
        *,
        tool_call: dict[str, Any],
        loop_index: int,
        run: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """A call the loop answers itself, recorded like a tool's.

        Its ``execute_tool.start`` and ``execute_tool.end`` are what a
        recovered request reads the result from.
        """
        ids = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "loop_index": loop_index,
            "tool_call": _tool_call(tool_call),
        }
        await record_tool_start(self.runtime, **ids)
        result_payload = await run()
        await record_tool_end(self.runtime, **ids, result_payload=result_payload)
        return {
            **state,
            "result_payloads": list(state.get("result_payloads") or []) + [result_payload],
        }

    async def _tool_call(
        self,
        state: dict[str, Any],
        *,
        loop_index: int,
        call_index: int,
        tool_call: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(tool_call.get("name") or "")
        if name == FORWARD_ANSWER:
            return await self._loop_handled_call(
                state,
                tool_call=tool_call,
                loop_index=loop_index,
                run=lambda: self._forward_answer(state, tool_call=tool_call),
            )
        handler = {"AskUser": self._ask_user, "browser_handoff": self._browser_handoff}.get(name)
        if handler is not None:
            return await self._loop_handled_call(
                state,
                tool_call=tool_call,
                loop_index=loop_index,
                run=lambda: handler(
                    tool_call=tool_call, loop_index=loop_index, call_index=call_index
                ),
            )
        # A RunSubAgents or RunWorkflow call derives its child ids or run
        # dedup key from its position, so a rerun gets back the children or
        # run already started.
        prefix = self._call_prefix(name, loop_index, call_index)
        # Such a call only waits, on children that may need this agent's
        # slots or on a workflow run, so it gives its own back meanwhile.
        wait = self._slot_released() if prefix else contextlib.nullcontext()
        relay = ChildInteractionRelay(self) if name == "RunSubAgents" else None
        with bound_subagent_id_prefix(prefix), bound_interaction_relay(relay):
            async with wait:
                return await retry_transient(
                    lambda: run_step_tool_call(
                        self.runtime,
                        self.request_id,
                        self.session_id,
                        self.trace_id,
                        state,
                        tool_call,
                    )
                )

    def _call_prefix(self, name: str, loop_index: int, call_index: int) -> str | None:
        if name == "RunSubAgents":
            return f"{self.request_id}-sub-{loop_index}-{call_index}"
        if name == "RunWorkflow":
            return f"{self.request_id}-wf-{loop_index}-{call_index}"
        return None

    async def _act(self, state: dict[str, Any]) -> dict[str, Any]:
        """Answer the action's interaction, then run its tool calls in order.

        A maximal run of consecutive parallel-safe calls runs concurrently as
        one batch; any other call runs alone. A call whose result the state
        already holds is skipped: a recovered loop runs only the calls with no
        ``execute_tool.end``.
        """
        loop_index = int(state.get("loop_index") or 0)
        action = dict(state.get("action") or {})

        interaction = action.get("interaction")
        if isinstance(interaction, dict) and "interaction_response" not in state:
            self._check_cancelled()
            _, response, _ = await self._run_interaction(
                loop_index=loop_index,
                interaction_type=str(interaction.get("type") or "info"),
                prompt=str(interaction.get("prompt") or ""),
                options=interaction.get("options"),
                timeout_seconds=int(interaction.get("timeout_seconds") or 300),
                step_prefix="interaction",
            )
            state = {**state, "interaction_response": response}

        tool_calls = context_helpers.tool_calls_from_action_payload(action)
        raw_tool_calls = list(action.get("tool_calls") or [])
        # Only an explicit approve runs the tools; deny, skip and a timeout
        # (answered "deny") do not.
        approval_denied = (
            isinstance(interaction, dict)
            and interaction.get("type") == "approval"
            and state.get("interaction_response") != "approve"
        )
        done = {
            str(result.get("tool_call_id") or "")
            for result in list(state.get("result_payloads") or [])
        }

        def parallel_safe(index: int) -> bool:
            raw = raw_tool_calls[index] if index < len(raw_tool_calls) else None
            return bool(raw.get("parallel_safe")) if isinstance(raw, dict) else False

        def payload(index: int) -> dict[str, Any]:
            call = tool_calls[index]
            return {"id": call.id, "name": call.name, "arguments": dict(call.arguments or {})}

        total = len(tool_calls)
        index = 0
        while index < total:
            if tool_calls[index].id in done:
                index += 1
                continue
            self._check_cancelled()
            if approval_denied:
                call = tool_calls[index]
                denied = {
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "duration_ms": 0,
                    "result": {
                        "content": (
                            "Tool execution denied by user "
                            f"({state.get('interaction_response')})"
                        ),
                        "is_error": True,
                        "metadata": {"denied": True},
                    },
                }
                state = await self._loop_handled_call(
                    state,
                    tool_call=payload(index),
                    loop_index=loop_index,
                    run=lambda denied=denied: _resolved(denied),
                )
                index += 1
                continue
            end = index + 1
            if parallel_safe(index):
                while end < total and parallel_safe(end) and tool_calls[end].id not in done:
                    end += 1
            if end - index == 1:
                state = await self._tool_call(
                    state, loop_index=loop_index, call_index=index, tool_call=payload(index)
                )
            else:
                batch = [payload(k) for k in range(index, end)]
                for k, call in zip(range(index, end), batch):
                    prefix = self._call_prefix(call["name"], loop_index, k)
                    if prefix:
                        call["call_prefix"] = prefix
                wait = (
                    self._slot_released()
                    if any("call_prefix" in call for call in batch)
                    else contextlib.nullcontext()
                )
                async with wait:
                    state = await retry_transient(
                        lambda: run_step_tool_batch(
                            self.runtime,
                            self.request_id,
                            self.session_id,
                            self.trace_id,
                            state,
                            batch,
                        )
                    )
            index = end
        # Results in call order, whichever ran first, so each maps back to its call.
        order = {call.id: position for position, call in enumerate(tool_calls)}
        results = sorted(
            list(state.get("result_payloads") or []),
            key=lambda result: order.get(str(result.get("tool_call_id") or ""), total),
        )
        return {**state, "result_payloads": results}

    # --- the loop ------------------------------------------------------------

    async def run(self) -> None:
        metadata = self.request_metadata
        with bound_request_id(self.request_id), bound_session_id(
            self.session_id
        ), bound_host_id(host_id_from_request_metadata(metadata)), bound_workflow_ids(
            metadata.get("workflow_id"), metadata.get("workflow_run_id")
        ), bound_request_metadata(
            caller_metadata_from_request_metadata(metadata)
        ), bound_event_session_id(metadata.get("event_session_id")):
            try:
                await self._take_slot()
                await self._run()
            except RequestCancelled:
                # The cancel path already recorded the request as cancelled.
                return
            except Exception as exc:
                error_payload = classify_error(exc)
                await self._persist_failed_turn(error_payload)
                await fail_request(
                    self.runtime,
                    self.request_id,
                    self.session_id,
                    self.trace_id,
                    error_payload,
                )
            finally:
                self._give_slot()

    async def _persist_failed_turn(self, error_payload: dict[str, Any]) -> None:
        """Save the user's message of a failed request, before the failure is
        announced, so the next request on the session can see it.

        Only a request with no parent: a subagent's failure reaches its caller
        as a tool result, and a workflow task's turn is never replayed.
        """
        if self.request_metadata.get("parent"):
            return
        try:
            await persist_failed_turn(
                self.runtime,
                self.session_id,
                self.trace_id,
                self.message,
                error_payload,
                self.request_metadata,
            )
        except Exception:
            # The failure is still recorded; only the replayed turn is lost.
            log.exception("could not save the failed turn of request %s", self.request_id)

    async def _run(self) -> None:
        runtime = self.runtime
        request_id = self.request_id
        session_id = self.session_id
        # The trace was minted when the request was accepted; a recovered
        # request reads the same one.
        self.trace_id = await start_request_trace(runtime, request_id, session_id, self.message)
        trace_id = self.trace_id
        # The provider, tools and event logger read the trace from context.
        with bound_trace_id(trace_id):
            structured_output_request = self.request_metadata.get("structured_output_request")

            state = await rebuild_request_state(
                runtime,
                session_id=session_id,
                message=self.message,
                request_metadata=self.request_metadata,
                events=await runtime.runtime_store.list_request_events(request_id),
            )
            if state is None:
                state = await load_request_context(
                    runtime, request_id, session_id, trace_id, self.message, self.request_metadata
                )

            while not state.get("done"):
                self._check_cancelled()
                if state.get("action") is None:
                    planned = state
                    state = await retry_transient(
                        lambda: plan_request_step(
                            runtime, request_id, session_id, trace_id, planned
                        )
                    )
                state = await self._act(state)
                self._check_cancelled()
                state = await commit_request_step(
                    runtime, request_id, session_id, trace_id, state, structured_output_request
                )

            if (
                isinstance(structured_output_request, dict)
                and state.get("structured_output") is None
            ):
                # Not recorded: a recovered request runs the finalizer again.
                state = await finalize_structured_output(
                    runtime, request_id, session_id, trace_id, state, structured_output_request
                )
            # Saving a turn twice stores it once, so a recovered request that
            # saved it already gets the stored turn back.
            turn_payload = await persist_completed_turn(
                runtime,
                request_id,
                session_id,
                trace_id,
                self.message,
                state,
                self.request_metadata,
            )
            await complete_request(runtime, request_id, session_id, trace_id, turn_payload)


class ChildInteractionRelay:
    """Relays a RunSubAgents call's child interactions onto the calling request.

    Each interaction a child opens becomes an interaction of this request, the
    one the user talks to, with an id derived from the child's request id and
    interaction id and an ``origin`` naming the child. The event log is the
    only state: a create or an ack with an id already stored is a no-op.
    """

    def __init__(self, run: RequestRun) -> None:
        self._run = run

    async def open(self, interaction: dict[str, Any]) -> str:
        run = self._run
        interaction_id = relayed_interaction_id(
            str(interaction.get("request_id") or ""),
            str(interaction.get("interaction_id") or ""),
        )
        extra: dict[str, Any] = {
            "origin": {
                key: interaction.get(key)
                for key in ("agent_id", "request_id", "session_id", "trace_id", "interaction_id")
            }
        }
        interaction_type = str(interaction.get("type") or "info")
        schema = interaction.get("schema")
        await emit_interaction_create(
            run.runtime,
            run.request_id,
            run.session_id,
            run.trace_id,
            interaction_id=interaction_id,
            interaction_type=interaction_type,
            prompt=str(interaction.get("prompt") or ""),
            schema=schema if isinstance(schema, dict) else interaction_schema(interaction_type),
            timeout_seconds=int(interaction.get("timeout_seconds") or 0),
            extra=extra,
        )
        return interaction_id

    async def wait(self, interaction_id: str) -> dict[str, Any]:
        return await self._run._await_close(interaction_id)

    async def close(self, interaction_id: str, ack: dict[str, Any]) -> None:
        run = self._run
        await emit_interaction_ack(
            run.runtime,
            run.request_id,
            run.session_id,
            run.trace_id,
            interaction_id=interaction_id,
            response=ack.get("response"),
            timed_out=bool(ack.get("timed_out")),
            cancelled=bool(ack.get("cancelled")),
        )
        run.wake_interaction(interaction_id)


def relayed_interaction_id(child_request_id: str, child_interaction_id: str) -> str:
    """The id of a child's interaction as relayed onto the calling request."""
    return derive_interaction_id(f"{child_request_id}:{child_interaction_id}")


__all__ = ["ChildInteractionRelay", "RequestCancelled", "RequestRun", "relayed_interaction_id"]

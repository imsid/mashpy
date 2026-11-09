"""The request loop: one agent request, continued from its checkpoint.

The loop runs the same steps in the same order whether a request is new,
recovered after a crash, or resumed. Its state is one dict, the checkpoint,
saved to ``runtime_request.checkpoint`` after every step:

- ``trace_id``: the request's trace, read from its accepted event
- ``state``: the loop state the steps thread through each other (context,
  loop index, the model's action, the tool results so far)
- ``memo``: small results inside the current step, by name, such as an
  interaction's id or a browser handoff's link. Cleared on commit.

A step that was running when the process died runs again; the events it
appends are deduped by key.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...logging import (
    bound_host_id,
    bound_request_id,
    bound_request_metadata,
    bound_session_id,
    bound_subagent_id_prefix,
    bound_trace_id,
)
from ...logging.trace_context import bound_workflow_ids
from ...tools.subagent import bound_interaction_relay
from .. import context as context_helpers
from ..errors import classify_error, retry_transient
from ..events import RuntimeEventType
from ..requests import (
    caller_metadata_from_request_metadata,
    host_id_from_request_metadata,
)
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
    plan_request_step,
    run_step_tool_batch,
    run_step_tool_call,
    start_browser_handoff,
    start_request_trace,
)

if TYPE_CHECKING:
    from ..events import RuntimeEvent
    from ..service import AgentRuntime


class RequestCancelled(Exception):
    """Raised at a step boundary once the request has been cancelled."""


_CANCELLED = object()


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
        checkpoint: dict[str, Any] | None,
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self.runtime = runtime
        self.request_id = request_id
        self.message = message
        self.session_id = session_id
        self.request_metadata = dict(request_metadata or {})
        self.checkpoint: dict[str, Any] = dict(checkpoint or {})
        self.checkpoint.setdefault("memo", {})
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

    # --- checkpoint ----------------------------------------------------------

    @property
    def state(self) -> dict[str, Any] | None:
        return self.checkpoint.get("state")

    async def _save(self, state: dict[str, Any] | None = None) -> None:
        if state is not None:
            self.checkpoint["state"] = state
        await self.runtime.runtime_store.save_checkpoint(
            self.request_id, self.checkpoint
        )

    async def _step(
        self, name: str, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        """Run ``fn`` once per checkpoint: a recovered loop gets the stored result."""
        memo = self.checkpoint["memo"]
        if name in memo:
            return memo[name]
        result = await fn(*args, **kwargs)
        memo[name] = result
        await self._save()
        return result

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
        self, interaction_id: str, *, deadline: float, interaction_type: str
    ) -> tuple[Any, bool]:
        """Wait for the interaction's ``interaction.end``; append a timeout at the deadline.

        Returns ``(response, timed_out)``, or ``(_CANCELLED, False)`` when the
        interaction was cancelled.
        """
        waiter = asyncio.Event()
        self._interaction_waiters[interaction_id] = waiter
        try:
            while True:
                end = await self._interaction_end(interaction_id)
                if end is None and time.time() >= deadline:
                    end = await emit_interaction_ack(
                        self.runtime,
                        self.request_id,
                        self.session_id,
                        self.checkpoint.get("trace_id"),
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
                try:
                    await asyncio.wait_for(
                        waiter.wait(), timeout=max(0.0, deadline - time.time())
                    )
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
        timeout_seconds: int,
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
            opened = await self._step(
                step_name,
                self._open_interaction,
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
                    deadline=float(opened["deadline"]),
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
        timeout_seconds: int,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        await emit_interaction_create(
            self.runtime,
            self.request_id,
            self.session_id,
            self.checkpoint["trace_id"],
            interaction_id=interaction_id,
            interaction_type=interaction_type,
            prompt=prompt,
            schema=interaction_schema(interaction_type, options),
            timeout_seconds=timeout_seconds,
            extra=extra,
        )
        return {
            "interaction_id": interaction_id,
            "deadline": time.time() + timeout_seconds,
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
        state: dict[str, Any],
        *,
        tool_call: dict[str, Any],
        loop_index: int,
        call_index: int,
    ) -> dict[str, Any]:
        """Turn an AskUser tool call into an interaction."""
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
            **state,
            "result_payloads": list(state.get("result_payloads") or []) + [result_payload],
        }

    async def _browser_handoff(
        self,
        state: dict[str, Any],
        *,
        tool_call: dict[str, Any],
        loop_index: int,
        call_index: int,
    ) -> dict[str, Any]:
        """Turn browser_handoff into a live-view link and an interaction.

        The link is memoized, so a recovered request hands the user the same
        one. Ending the handoff revokes the link and returns the page as the
        user left it.
        """
        arguments = dict(tool_call.get("arguments") or {})
        reason = str(arguments.get("reason") or "The page needs you.").strip()
        started = await self._step(
            f"browser.handoff.start.{loop_index}.{call_index}",
            start_browser_handoff,
            self.runtime,
            self.request_id,
        )
        metadata: dict[str, Any] = {}
        images: list[dict[str, Any]] = []
        if "error" in started:
            content, is_error = str(started["error"]), True
        else:
            interaction_id, response, timed_out = await self._run_interaction(
                loop_index=loop_index,
                interaction_type="browser",
                prompt=reason,
                options=None,
                timeout_seconds=int(started.get("timeout_seconds") or 900),
                step_prefix=f"browser.handoff.{call_index}",
                extra={"url": started["url"], "expires_at": started["expires_at"]},
            )
            page = await self._step(
                f"browser.handoff.end.{loop_index}.{call_index}",
                end_browser_handoff,
                self.runtime,
                self.request_id,
            )
            metadata = {"interaction_id": interaction_id, "timed_out": timed_out}
            if response == "done" and not timed_out:
                content = (
                    "The user is done in the browser. The page now:\n"
                    f"{page.get('content') or ''}"
                )
                is_error = bool(page.get("is_error"))
                images = list(page.get("images") or [])
            elif timed_out:
                content = "The user did not finish in the browser before the link expired."
                is_error = True
            else:
                content, is_error = "The user cancelled the handoff.", True
        result: dict[str, Any] = {"content": content, "is_error": is_error, "metadata": metadata}
        if images:
            result["images"] = images
        result_payload = {
            "tool_call_id": tool_call.get("id", ""),
            "tool_name": "browser_handoff",
            "duration_ms": 0,
            "result": result,
        }
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
        if name == "AskUser":
            return await self._ask_user(
                state, tool_call=tool_call, loop_index=loop_index, call_index=call_index
            )
        if name == "browser_handoff":
            return await self._browser_handoff(
                state, tool_call=tool_call, loop_index=loop_index, call_index=call_index
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
                        self.checkpoint["trace_id"],
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
        one batch; any other call runs alone. The checkpoint is saved after
        each call or batch, and a recovered loop skips the calls whose results
        it already has.
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
            await self._save(state)

        tool_calls = context_helpers.tool_calls_from_action_payload(action)
        raw_tool_calls = list(action.get("tool_calls") or [])
        # Only an explicit approve runs the tools; deny, skip and a timeout
        # (answered "deny") do not.
        approval_denied = (
            isinstance(interaction, dict)
            and interaction.get("type") == "approval"
            and state.get("interaction_response") != "approve"
        )

        def parallel_safe(index: int) -> bool:
            raw = raw_tool_calls[index] if index < len(raw_tool_calls) else None
            return bool(raw.get("parallel_safe")) if isinstance(raw, dict) else False

        def payload(index: int) -> dict[str, Any]:
            call = tool_calls[index]
            return {"id": call.id, "name": call.name, "arguments": dict(call.arguments or {})}

        total = len(tool_calls)
        index = len(list(state.get("result_payloads") or []))
        while index < total:
            self._check_cancelled()
            results = list(state.get("result_payloads") or [])
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
                state = {**state, "result_payloads": results + [denied]}
                index += 1
                continue
            end = index + 1
            if parallel_safe(index):
                while end < total and parallel_safe(end):
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
                            self.checkpoint["trace_id"],
                            state,
                            batch,
                        )
                    )
            index = end
            await self._save(state)
        return state

    # --- the loop ------------------------------------------------------------

    async def run(self) -> None:
        metadata = self.request_metadata
        with bound_request_id(self.request_id), bound_session_id(
            self.session_id
        ), bound_host_id(host_id_from_request_metadata(metadata)), bound_workflow_ids(
            metadata.get("workflow_id"), metadata.get("workflow_run_id")
        ), bound_request_metadata(caller_metadata_from_request_metadata(metadata)):
            try:
                await self._take_slot()
                await self._run()
            except RequestCancelled:
                # The cancel path already recorded the request as cancelled.
                return
            except Exception as exc:
                await fail_request(
                    self.runtime,
                    self.request_id,
                    self.session_id,
                    self.checkpoint.get("trace_id"),
                    classify_error(exc),
                )
            finally:
                self._give_slot()

    async def _run(self) -> None:
        runtime = self.runtime
        request_id = self.request_id
        session_id = self.session_id
        if not self.checkpoint.get("trace_id"):
            self.checkpoint["trace_id"] = await start_request_trace(
                runtime, request_id, session_id, self.message
            )
            await self._save()
        trace_id = self.checkpoint["trace_id"]
        # The provider, tools and event logger read the trace from context.
        with bound_trace_id(trace_id):
            structured_output_request = self.request_metadata.get("structured_output_request")

            state = self.state
            if state is None:
                state = await load_request_context(
                    runtime, request_id, session_id, trace_id, self.message, self.request_metadata
                )
                await self._save(state)

            while not state.get("done"):
                self._check_cancelled()
                if state.get("action") is None:
                    planned = state
                    state = await retry_transient(
                        lambda: plan_request_step(
                            runtime, request_id, session_id, trace_id, planned
                        )
                    )
                    await self._save(state)
                state = await self._act(state)
                self._check_cancelled()
                state = await commit_request_step(
                    runtime, request_id, session_id, trace_id, state, structured_output_request
                )
                self.checkpoint["memo"] = {}
                await self._save(state)

            if (
                isinstance(structured_output_request, dict)
                and state.get("structured_output") is None
            ):
                state = await finalize_structured_output(
                    runtime, request_id, session_id, trace_id, state, structured_output_request
                )
                await self._save(state)
            turn_payload = await self._step(
                "turn.persist",
                persist_completed_turn,
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
        for key in ("url", "expires_at"):
            if key in interaction:
                extra[key] = interaction[key]
        interaction_type = str(interaction.get("type") or "info")
        schema = interaction.get("schema")
        await emit_interaction_create(
            run.runtime,
            run.request_id,
            run.session_id,
            run.checkpoint["trace_id"],
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
            run.checkpoint["trace_id"],
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

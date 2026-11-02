"""The request engine: runs an agent's requests in its process.

Each request runs as an asyncio task over a ``RequestRun``. Its durable state
is the request's row in ``runtime_request`` (status and checkpoint) and its
events, so the engine holds nothing that a restart would lose:

- ``open`` continues every request of the agent whose status is ``running``.
  That one query is all recovery costs; an idle engine makes no others.
- ``cancel_request`` records the request cancelled and stops its loop at the
  next step boundary.
- ``continue_request`` runs a cancelled or failed request again from its
  checkpoint, after ``resume_request`` has recorded it running.
- ``answer_interaction`` stores the answer and wakes the loop waiting on it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..events import RequestStatus, RuntimeEventType
from ..requests import find_pending_interaction
from .loop import RequestRun
from .steps import emit_interaction_ack, emit_request_cancelled, fail_request

if TYPE_CHECKING:
    from ..service import AgentRuntime

logger = logging.getLogger(__name__)

# A request continued this many times after a crash without finishing fails,
# so a request that crashes its process every time cannot loop forever.
MAX_RECOVERIES = 10


class InteractionClosedError(RuntimeError):
    """Raised when an answer names an interaction that is not open."""


class RequestEngine:
    def __init__(self, runtime: "AgentRuntime") -> None:
        self._runtime = runtime
        self._runs: dict[str, RequestRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def _store(self) -> Any:
        return self._runtime.runtime_store

    async def open(self) -> None:
        for request_id in await self._store.list_running_requests(self._runtime.app_id):
            if request_id in self._tasks:
                continue
            try:
                await self._recover(request_id)
            except Exception:
                logger.exception("could not recover request %s", request_id)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        # A cancelled task leaves its request running, so the next open
        # continues it.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._runs.clear()

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
    ) -> None:
        run = RequestRun(
            self._runtime,
            request_id=request_id,
            message=message,
            session_id=session_id,
            request_metadata=request_metadata,
            checkpoint=None,
        )
        self._runs[request_id] = run
        self._spawn(request_id, run.run())

    async def continue_request(self, request_id: str) -> None:
        """Run the request again from its checkpoint, once it is recorded running.

        A cancelled loop may still be finishing its last step; the new loop
        starts after it, from the checkpoint that step leaves.
        """
        previous = self._tasks.get(request_id)
        run = self._runs.get(request_id)
        if previous is not None and run is not None and not run.cancelled.is_set():
            return
        self._spawn(request_id, self._continue(request_id, previous))

    async def get_request_status(self, *, request_id: str) -> dict[str, Any]:
        status = await self._store.get_request_lifecycle(request_id)
        if status is None:
            raise KeyError(f"request '{request_id}' not found")
        return {"request_id": request_id, "status": status.value}

    async def cancel_request(self, *, request_id: str) -> dict[str, Any]:
        status = await self._store.get_request_lifecycle(request_id)
        if status is None:
            raise KeyError(f"request '{request_id}' not found")
        if status.terminal:
            return {
                "request_id": request_id,
                "status": status.value,
                "message": "request is already in a terminal state",
            }
        run = self._runs.get(request_id)
        events = await self._store.list_request_events(request_id)
        trace_id = next((e.trace_id for e in events if e.trace_id), None)
        session_id = next((e.session_id for e in events if e.session_id), None)
        pending_interaction_id = find_pending_interaction(events)
        if pending_interaction_id is not None:
            # Clears the prompt. A later resume opens a new interaction.
            await emit_interaction_ack(
                self._runtime,
                request_id,
                session_id,
                trace_id,
                interaction_id=pending_interaction_id,
                response=None,
                cancelled=True,
            )
        await emit_request_cancelled(self._runtime, request_id, session_id, trace_id)
        if run is not None:
            # The step running now finishes; the loop stops before the next.
            run.cancel()
        return {
            "request_id": request_id,
            "status": RequestStatus.CANCELLED.value,
            "message": "request has been cancelled",
        }

    async def answer_interaction(
        self, *, request_id: str, interaction_id: str, response: Any
    ) -> None:
        """Store the answer to an open interaction and wake the request.

        Raises ``KeyError`` for an unknown interaction and
        ``InteractionClosedError`` once it has an answer, a timeout or a cancel.
        """
        events = await self._store.list_request_events(request_id)
        opened = next(
            (
                event
                for event in events
                if event.event_type == RuntimeEventType.INTERACTION_START.value
                and (event.payload or {}).get("interaction_id") == interaction_id
            ),
            None,
        )
        if opened is None:
            raise KeyError(f"interaction '{interaction_id}' not found")
        stored = await emit_interaction_ack(
            self._runtime,
            request_id,
            opened.session_id,
            opened.trace_id,
            interaction_id=interaction_id,
            response=response,
        )
        payload = stored.payload or {}
        if (
            payload.get("cancelled")
            or payload.get("timed_out")
            or payload.get("response") != response
        ):
            raise InteractionClosedError(
                f"interaction '{interaction_id}' is no longer open"
            )
        run = self._runs.get(request_id)
        if run is not None:
            run.wake_interaction(interaction_id)

    # --- internals -----------------------------------------------------------

    async def _accepted(self, request_id: str) -> dict[str, Any]:
        events = await self._store.list_request_events(request_id)
        accepted = next(
            (e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value),
            None,
        )
        if accepted is None:
            raise KeyError(f"request '{request_id}' not found")
        payload = accepted.payload or {}
        return {
            "message": str(payload.get("message") or ""),
            "session_id": str(payload.get("initial_session_id") or accepted.session_id or ""),
            "request_metadata": dict(payload.get("request_metadata") or {}),
            "trace_id": accepted.trace_id,
        }

    async def _recover(self, request_id: str) -> None:
        recoveries = await self._store.count_recovery(request_id)
        if recoveries > MAX_RECOVERIES:
            accepted = await self._accepted(request_id)
            await fail_request(
                self._runtime,
                request_id,
                accepted["session_id"],
                accepted["trace_id"],
                {
                    "error": (
                        f"the request did not finish after {MAX_RECOVERIES} "
                        "recoveries; rerun it to start over"
                    ),
                    "error_type": "recovery_limit",
                    "retryable": False,
                },
            )
            return
        await self.continue_request(request_id)

    async def _continue(
        self, request_id: str, previous: "asyncio.Task[None] | None"
    ) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        if await self._store.get_request_lifecycle(request_id) is not RequestStatus.RUNNING:
            return
        accepted = await self._accepted(request_id)
        run = RequestRun(
            self._runtime,
            request_id=request_id,
            message=accepted["message"],
            session_id=accepted["session_id"],
            request_metadata=accepted["request_metadata"],
            checkpoint=await self._store.get_checkpoint(request_id),
        )
        self._runs[request_id] = run
        await run.run()

    def _spawn(self, request_id: str, coro: Any) -> None:
        task = asyncio.create_task(
            coro, name=f"mash-request-{self._runtime.app_id}-{request_id}"
        )
        self._tasks[request_id] = task

        def _done(_: asyncio.Task[None]) -> None:
            if self._tasks.get(request_id) is task:
                self._tasks.pop(request_id, None)
                self._runs.pop(request_id, None)

        task.add_done_callback(_done)


__all__ = ["InteractionClosedError", "MAX_RECOVERIES", "RequestEngine"]

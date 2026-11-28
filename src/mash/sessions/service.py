"""EventService: the session queues, run by the host.

An event is inserted ``queued``. Dequeue starts a session's oldest queued
event when the session has none started and its host is defined, and submits
it to the host's companion as a request whose id is derived from the session
and event ids. The companion routes the message: it answers itself, or starts
one agent's request in the session and ends with ``routed`` naming it. A
watcher per started event follows the companion's request, then the routed
request, to its end, copies the outcome onto the row, and dequeues the next
event. Every step is idempotent, so the host can restart at any point: on
start it watches every started event again and dequeues every session with
queued events.

The event's stream, answers and handoffs reach the routed request directly,
through the host, with no model call. Its frames follow the companion's,
numbered on from the companion's last, so a reader resumes with one ``after``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from mash.runtime.client import (
    AgentClientError,
    AgentRequestRefusedError,
    AgentUnavailableError,
)
from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import pending_interactions

from .models import (
    CANCELLED,
    CLOSED,
    COMPLETED,
    FAILED,
    QUEUED,
    RUNNING,
    STARTED,
    WAITING,
    EventRecord,
    EventNotFoundError,
    EventTerminalError,
    SessionRecord,
    event_request_id,
)
from .store import EventStore

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool

logger = logging.getLogger(__name__)

END_FRAMES = {
    "request.completed": COMPLETED,
    "request.error": FAILED,
    "request.cancelled": CANCELLED,
}
_FRAME_FOR_STATUS = {status: frame for frame, status in END_FRAMES.items()}
# A watcher retries a stream or a submit that failed this long after it,
# doubling up to the cap.
_RETRY_BASE_SECONDS = 0.5
_RETRY_MAX_SECONDS = 30.0
KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class NewEvent:
    """A new event's body, checked by the route."""

    event_id: str
    host_id: str
    app_id: str
    policy_id: str
    message: str
    context: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    structured_output: Optional[dict[str, Any]] = None
    # The poster's W3C trace headers, so the request joins the caller's trace.
    traceparent: Optional[str] = None
    tracestate: Optional[str] = None

    def body(self) -> dict[str, Any]:
        stored: dict[str, Any] = {"app_id": self.app_id, "message": self.message}
        for key in ("context", "metadata", "structured_output", "traceparent", "tracestate"):
            value = getattr(self, key)
            if value is not None:
                stored[key] = value
        return stored


class EventService:
    def __init__(self, pool: "Pool", store: EventStore) -> None:
        self._pool = pool
        self._store = store
        self._watchers: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Each session's change signal, set and replaced when one of its
        # events changes status, so every stream waiting on it wakes.
        self._changes: dict[str, asyncio.Event] = {}
        # Events cancelled before their request was submitted.
        self._cancelling: set[tuple[str, str]] = set()
        self._dequeue_locks: dict[str, asyncio.Lock] = {}
        self._started = False

    @property
    def store(self) -> EventStore:
        return self._store

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Watch every started event and dequeue every session with queued ones."""
        self._started = True
        after = 0
        while True:
            page = await self._store.list_events(statuses=[STARTED], after_seq=after, limit=500)
            for record in page:
                self._watch(record)
            if len(page) < 500:
                break
            after = page[-1].seq
        for session_id in await self._store.sessions_with_queued():
            await self.dequeue(session_id)

    async def close(self) -> None:
        self._started = False
        tasks = list(self._watchers.values())
        self._watchers.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def host_defined(self, host_id: str) -> None:
        """Dequeue the host's sessions, whose events may have waited for it."""
        if not self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._dequeue_host(host_id), name=f"mash-dequeue-{host_id}")

    async def _dequeue_host(self, host_id: str) -> None:
        for session_id in await self._store.sessions_with_queued(host_id):
            await self.dequeue(session_id)

    # --- events --------------------------------------------------------------

    async def post_event(self, session_id: str, new: NewEvent) -> tuple[EventRecord, bool]:
        """Insert a new event, or return the row of a seen one; ``(record, created)``.

        A seen ``(session_id, event_id)`` returns its row whatever the body.
        Raises ``ValueError`` for an unknown host, ``PolicyNotFoundError`` and
        ``SessionMismatchError``.
        """
        existing = await self._store.get_event(session_id, new.event_id)
        if existing is not None:
            return existing, False
        self._pool.get_host(new.host_id)
        record, created = await self._store.insert_event(
            session_id,
            new.event_id,
            host_id=new.host_id,
            policy_id=new.policy_id,
            body=new.body(),
        )
        if created:
            self._changed(session_id)
            await self.dequeue(session_id)
            record = await self._store.get_event(session_id, new.event_id) or record
        return record, created

    async def dequeue(self, session_id: str) -> None:
        """Start the session's next event, if it may start, and watch it."""
        lock = self._dequeue_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            claimed = await self._store.claim_next(session_id, self._host_is_defined)
            if claimed is None:
                return
            record, session, policy = claimed
            self._changed(session_id)
            self._watch(record, session=session, policy=policy)

    def _host_is_defined(self, host_id: str) -> bool:
        try:
            self._pool.get_host(host_id)
        except ValueError:
            return False
        return True

    async def cancel(self, session_id: str, event_id: str) -> EventRecord:
        """Cancel a queued event, or the request of a started one and its children.

        Raises ``EventNotFoundError`` and ``EventTerminalError``.
        """
        record = await self._require(session_id, event_id)
        if record.status == QUEUED:
            cancelled = await self._store.cancel_queued(session_id, event_id)
            if cancelled is not None:
                self._changed(session_id)
                return cancelled
            record = await self._require(session_id, event_id)
        if record.status != STARTED:
            raise EventTerminalError(f"event '{event_id}' is {record.status}")
        request_id = str(record.request_id)
        runtime_store = self._pool.get_runtime_store()
        if runtime_store is not None and not await runtime_store.has_request(request_id):
            # Started, but its request not submitted yet: the watcher closes it
            # instead of submitting.
            self._cancelling.add((session_id, event_id))
        else:
            await self._cancel_tree(request_id)
        return record

    async def answer(
        self, session_id: str, event_id: str, interaction_id: str, response: Any
    ) -> EventRecord:
        """Forward an answer to the request that asked: the companion's or the
        routed one. The request's errors propagate.

        Raises ``EventNotFoundError``, and ``KeyError`` when the event has no
        request yet or none of its requests has such an interaction.
        """
        record = await self._require(session_id, event_id)
        if not record.request_id:
            raise KeyError(f"interaction '{interaction_id}' not found")
        runtime_store = self._pool.get_runtime_store()
        for agent_id, request_id in await self._requests(record):
            events = await runtime_store.list_request_events(request_id)
            if any(
                e.event_type == RuntimeEventType.INTERACTION_START.value
                and (e.payload or {}).get("interaction_id") == interaction_id
                for e in events
            ):
                await self._pool.get_client(agent_id).post_interaction(
                    request_id, interaction_id=interaction_id, response=response
                )
                return record
        raise KeyError(f"interaction '{interaction_id}' not found")

    async def handoff_target(
        self, session_id: str, event_id: str, interaction_id: str
    ) -> tuple[str, str]:
        """``(policy_id, request_id)`` whose browser an open handoff shows.

        The request is the routed one that asked. Raises
        ``EventNotFoundError``, and ``KeyError`` unless one of the event's
        requests has that ``browser`` interaction open.
        """
        record = await self._require(session_id, event_id)
        if not record.request_id:
            raise KeyError(interaction_id)
        runtime_store = self._pool.get_runtime_store()
        for _, request_id in await self._requests(record):
            events = await runtime_store.list_request_events(request_id)
            if interaction_id in pending_interactions(events):
                break
        else:
            raise KeyError(interaction_id)
        opened = next(
            dict(e.payload or {})
            for e in events
            if e.event_type == RuntimeEventType.INTERACTION_START.value
            and (e.payload or {}).get("interaction_id") == interaction_id
        )
        if opened.get("type") != "browser":
            raise KeyError(interaction_id)
        identity = await runtime_store.get_request_identity(request_id)
        if identity is None or not identity.policy_id:
            raise KeyError(interaction_id)
        return identity.policy_id, request_id

    async def _require(self, session_id: str, event_id: str) -> EventRecord:
        record = await self._store.get_event(session_id, event_id)
        if record is None:
            raise EventNotFoundError(session_id, event_id)
        return record

    # --- reading -------------------------------------------------------------

    async def describe(self, record: EventRecord) -> dict[str, Any]:
        """The event's state as the API reports it.

        A started event is ``running``, or ``waiting`` while its request has an
        open interaction; both are read from the request's log.
        """
        status = record.status
        interactions: list[dict[str, Any]] = []
        if status == STARTED and record.request_id:
            for _, request_id in await self._requests(record):
                interactions += await self._open_interactions(request_id)
            status = WAITING if interactions else RUNNING
        return {
            "session_id": record.session_id,
            "event_id": record.event_id,
            "request_id": self._request_id(record),
            "seq": record.seq,
            "status": status,
            "app_id": record.app_id,
            "interactions": interactions,
            "response": record.response,
            "error": record.error,
            "received_at": record.received_at,
            "started_at": record.started_at,
            "closed_at": record.closed_at,
        }

    @staticmethod
    def _request_id(record: EventRecord) -> str | None:
        """The row's request id; a queued event's is derived, as it will be."""
        if record.request_id:
            return record.request_id
        if record.status == QUEUED:
            return event_request_id(record.session_id, record.event_id)
        return None

    async def _open_interactions(self, request_id: str) -> list[dict[str, Any]]:
        runtime_store = self._pool.get_runtime_store()
        if runtime_store is None:
            return []
        events = await runtime_store.list_request_events(request_id)
        opened = {
            str((e.payload or {}).get("interaction_id")): dict(e.payload or {})
            for e in events
            if e.event_type == RuntimeEventType.INTERACTION_START.value
        }
        return [
            {
                key: value
                for key, value in opened[interaction_id].items()
                if key != "attributes"
            }
            for interaction_id in pending_interactions(events)
            if interaction_id in opened
        ]

    async def list_events(
        self,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        statuses: set[str] | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Events in seq order, filtered on the reported status."""
        row_statuses: set[str] | None = None
        if statuses is not None:
            row_statuses = {s for s in statuses if s not in (RUNNING, WAITING)}
            if statuses & {RUNNING, WAITING}:
                row_statuses.add(STARTED)
        described: list[dict[str, Any]] = []
        cursor = after_seq
        while len(described) < limit:
            page = await self._store.list_events(
                session_id=session_id,
                host_id=host_id,
                statuses=row_statuses,
                after_seq=cursor,
                limit=limit,
            )
            for record in page:
                state = await self.describe(record)
                if statuses is None or state["status"] in statuses:
                    described.append(state)
                    if len(described) == limit:
                        break
            if len(page) < limit:
                break
            cursor = page[-1].seq
        return described

    async def stream(
        self, session_id: str, event_id: str, *, after: int = 0
    ) -> AsyncIterator[tuple[str | None, Any, int | None]]:
        """The event's frames: ``(event name, data, seq)``; a None name is a keepalive.

        While the event is queued: one ``event.queued`` frame and keepalives.
        Once it starts: its request's frames from seq ``after``, to the end
        frame. An event cancelled while queued sends one ``event.cancelled``;
        a closed event whose request is gone sends one end frame from its row.
        """
        announced = False
        while True:
            # Taken before the read, so a change after the read still wakes it.
            changed = self._changes.setdefault(session_id, asyncio.Event())
            record = await self._require(session_id, event_id)
            if record.status == QUEUED:
                if not announced:
                    announced = True
                    yield "event.queued", await self.describe(record), None
                timeout = KEEPALIVE_SECONDS
            elif record.status == STARTED and not await self._has_request(record):
                # Started, its request about to be submitted.
                timeout = 0.5
            else:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout)
            if not changed.is_set() and record.status == QUEUED:
                yield None, None, None
        if record.status == CANCELLED and not record.request_id:
            yield "event.cancelled", await self.describe(record), None
            return
        if record.status in CLOSED and not await self._has_request(record):
            # Retention deleted the request; the row keeps its outcome.
            yield _FRAME_FOR_STATUS[record.status], {
                "request_id": record.request_id,
                "session_id": record.session_id,
                "event_id": record.event_id,
                "response": record.response,
                **(record.error or {}),
            }, None
            return
        async for frame in self._frames(record, after=after):
            name = str(frame.get("event") or "message")
            yield name, frame.get("data"), frame.get("seq")
            if name in END_FRAMES:
                return

    async def _routed(self, record: EventRecord) -> tuple[str, str, int] | None:
        """``(agent_id, request_id, offset)`` of the request the companion routed
        the event to, once the companion's request has ended with ``routed``.

        ``offset`` is the seq of the companion's end event: the routed
        request's frames are numbered on from it.
        """
        runtime_store = self._pool.get_runtime_store()
        if runtime_store is None or not record.request_id:
            return None
        events = await runtime_store.list_request_events(record.request_id)
        end = next(
            (e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_END.value),
            None,
        )
        if end is None:
            return None
        payload = end.payload or {}
        routed = ((payload.get("response") or {}).get("metadata") or {}).get("routed")
        if payload.get("status") != "ok" or not isinstance(routed, dict):
            return None
        return str(routed["agent_id"]), str(routed["request_id"]), int(end.request_seq or 0)

    async def _requests(self, record: EventRecord) -> list[tuple[str, str]]:
        """``(agent_id, request_id)`` of the event's requests: the companion's,
        and the routed one once there is one."""
        from mash.agents.companion import COMPANION_AGENT_ID

        requests = [(COMPANION_AGENT_ID, str(record.request_id))]
        routed = await self._routed(record)
        if routed is not None:
            requests.append((routed[0], routed[1]))
        return requests

    async def _frames(self, record: EventRecord, *, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        """The event's request frames after seq ``after``, to the end frame.

        The companion's frames come first. When its request ends with
        ``routed``, its end frame is left out and the routed request's frames
        follow, each seq raised by the companion's end seq.
        """
        routed = await self._routed(record)
        if routed is None or after < routed[2]:
            async for frame in self._companion().stream_response(
                str(record.request_id), after=after
            ):
                name = str(frame.get("event") or "")
                if name == "request.completed":
                    routed = routed or await self._routed(record)
                    if routed is not None:
                        break
                yield frame
                if name in END_FRAMES:
                    return
            if routed is None:
                return
        agent_id, request_id, offset = routed
        async for frame in self._pool.get_client(agent_id).stream_response(
            request_id, after=max(0, after - offset)
        ):
            if frame.get("seq") is not None:
                frame = {**frame, "seq": int(frame["seq"]) + offset}
            yield frame
            if str(frame.get("event") or "") in END_FRAMES:
                return

    async def _has_request(self, record: EventRecord) -> bool:
        runtime_store = self._pool.get_runtime_store()
        return bool(
            record.request_id
            and runtime_store is not None
            and await runtime_store.has_request(record.request_id)
        )

    # --- the watcher ---------------------------------------------------------

    async def _cancel_tree(self, request_id: str) -> None:
        """Cancel the event's request and its running children."""
        from mash.agents.companion import COMPANION_AGENT_ID

        await self._pool.cancel_with_children(COMPANION_AGENT_ID, request_id)

    def _companion(self) -> Any:
        from mash.agents.companion import COMPANION_AGENT_ID

        return self._pool.get_client(COMPANION_AGENT_ID)

    def _changed(self, session_id: str) -> None:
        signal = self._changes.pop(session_id, None)
        if signal is not None:
            signal.set()

    def _watch(
        self,
        record: EventRecord,
        *,
        session: SessionRecord | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        key = (record.session_id, record.event_id)
        current = self._watchers.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.get_running_loop().create_task(
            self._follow(record, session, policy),
            name=f"mash-event-{record.session_id}-{record.event_id}",
        )
        self._watchers[key] = task
        task.add_done_callback(lambda _: self._watchers.pop(key, None))

    async def _follow(
        self,
        record: EventRecord,
        session: SessionRecord | None,
        policy: dict[str, Any] | None,
    ) -> None:
        """Follow a started event's requests to the end, then close it and dequeue.

        The companion's request, then the request it routed to: the event's
        outcome is the routed request's.
        """
        key = (record.session_id, record.event_id)
        request_id = str(record.request_id)
        delay = _RETRY_BASE_SECONDS
        while True:
            try:
                runtime_store = self._pool.get_runtime_store()
                if not await runtime_store.has_request(request_id):
                    if key in self._cancelling:
                        self._cancelling.discard(key)
                        await self._close(record, CANCELLED, None, None)
                        return
                    await self._submit(record, session, policy)
                    if key in self._cancelling:
                        # Cancelled while it was being submitted.
                        self._cancelling.discard(key)
                        await self._cancel_tree(request_id)
                async for frame in self._frames(record):
                    name = str(frame.get("event") or "")
                    if name in END_FRAMES:
                        data = frame.get("data") or {}
                        error = (
                            {"error": data.get("error"), "error_type": data.get("error_type")}
                            if name == "request.error"
                            else None
                        )
                        await self._close(record, END_FRAMES[name], data.get("response"), error)
                        return
                    delay = _RETRY_BASE_SECONDS
            except asyncio.CancelledError:
                raise
            except AgentRequestRefusedError as exc:
                logger.error(
                    "event %s/%s: the companion refused its request: %s",
                    record.session_id,
                    record.event_id,
                    exc,
                )
                await self._close(
                    record, FAILED, None, {"error": str(exc), "error_type": "refused"}
                )
                return
            except Exception as exc:  # noqa: BLE001 - the watcher outlives restarts
                level = logging.INFO if isinstance(exc, AgentUnavailableError) else logging.WARNING
                logger.log(
                    level,
                    "event %s/%s: following its request failed, retrying in %.1fs: %s",
                    record.session_id,
                    record.event_id,
                    delay,
                    exc,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)

    async def _submit(
        self,
        record: EventRecord,
        session: SessionRecord | None,
        policy: dict[str, Any] | None,
    ) -> None:
        """Submit the event's request; idempotent on its request id."""
        if session is None:
            session = await self._store.get_session(record.session_id)
            if session is None:
                raise AgentClientError(f"session '{record.session_id}' not found")
        body = record.body
        await self._pool.submit_host_request(
            session.host_id,
            message=str(body.get("message") or ""),
            session_id=record.session_id,
            app_id=record.app_id,
            policy_id=session.policy_id,
            policy=policy,
            structured_output=body.get("structured_output"),
            context=body.get("context"),
            metadata=body.get("metadata"),
            request_id=str(record.request_id),
            traceparent=body.get("traceparent"),
            tracestate=body.get("tracestate"),
        )

    async def _close(
        self, record: EventRecord, status: str, response: Any, error: Any
    ) -> None:
        await self._store.close_event(
            record.session_id, record.event_id, status=status, response=response, error=error
        )
        self._changed(record.session_id)
        await self.dequeue(record.session_id)

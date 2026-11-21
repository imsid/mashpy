"""EventService over a fake pool: restart recovery, resubmission and refusals."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from mash.policies import PolicyStore
from mash.runtime.client import AgentRequestRefusedError
from mash.sessions import EventService, EventStore, NewEvent, event_request_id

URL = "postgresql://test/runtime"


class _Requests:
    """The companion's requests: which exist, and their streams' end frames."""

    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.submitted: list[str] = []
        self.refuse: set[str] = set()
        self.cancelled: list[str] = []

    async def has_request(self, request_id: str) -> bool:
        return request_id in self.existing

    async def list_request_events(self, request_id: str) -> list:
        return []


class _Companion:
    def __init__(self, requests: _Requests) -> None:
        self._requests = requests

    async def stream_response(self, request_id: str, *, after: int = 0):
        yield {"event": "request.accepted", "data": {"request_id": request_id}, "seq": 1}
        await asyncio.sleep(0)
        yield {"event": "request.completed", "data": {"response": {"text": request_id}}, "seq": 2}

    async def cancel_request(self, request_id: str) -> dict:
        self._requests.cancelled.append(request_id)
        return {}


class _Pool:
    def __init__(self, requests: _Requests) -> None:
        self.requests = requests

    def get_host(self, host_id: str):
        if host_id != "assistant":
            raise ValueError(f"host '{host_id}' not found")
        return host_id

    def get_runtime_store(self) -> _Requests:
        return self.requests

    def get_client(self, agent_id: str) -> _Companion:
        return _Companion(self.requests)

    async def cancel_with_children(self, agent_id: str, request_id: str) -> dict:
        self.requests.cancelled.append(request_id)
        return {}

    async def submit_host_request(self, host_id: str, *, request_id: str, **kwargs: Any) -> dict:
        if request_id in self.requests.refuse:
            raise AgentRequestRefusedError("POST request refused (status=400): bad")
        self.requests.submitted.append(request_id)
        self.requests.existing.add(request_id)
        return {"request_id": request_id}


def _new(event_id: str) -> NewEvent:
    return NewEvent(
        event_id=event_id, host_id="assistant", app_id="app-1", policy_id="p-1", message=event_id
    )


class EventServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests = _Requests()
        self.store = EventStore(URL, PolicyStore(URL))

    async def _until(self, check, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not await check():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("condition never held")
            await asyncio.sleep(0.01)

    async def _statuses(self) -> list[str]:
        return [e.status for e in await self.store.list_events(session_id="s-1")]

    async def test_a_restarted_host_follows_started_events_and_drains_the_queue(self) -> None:
        # Before the restart: e-1 started and its request exists; e-2 started
        # but its request never landed; e-3 queued.
        for event_id in ("e-1", "e-2", "e-3"):
            await self.store.insert_event(
                "s-1", event_id, host_id="assistant", policy_id="p-1", body=_new(event_id).body()
            )
        await self.store.claim_next("s-1", lambda _: True)
        self.requests.existing.add(event_request_id("s-1", "e-1"))

        service = EventService(_Pool(self.requests), self.store)
        await service.start()
        await self._until(lambda: self._done(3))
        await service.close()

        rows = await self.store.list_events(session_id="s-1")
        self.assertEqual([r.status for r in rows], ["completed"] * 3)
        self.assertEqual([r.response["text"] for r in rows], [r.request_id for r in rows])
        # e-1's request existed: only e-2 and e-3 were submitted, once each.
        self.assertEqual(
            self.requests.submitted,
            [event_request_id("s-1", "e-2"), event_request_id("s-1", "e-3")],
        )
        starts = [r.started_at for r in rows]
        self.assertEqual(starts, sorted(starts))

    async def _done(self, count: int) -> bool:
        return (await self._statuses()).count("completed") == count

    async def test_a_refused_submit_fails_the_event_and_the_next_one_starts(self) -> None:
        self.requests.refuse.add(event_request_id("s-1", "e-1"))
        service = EventService(_Pool(self.requests), self.store)
        await service.start()
        await service.post_event("s-1", _new("e-1"))
        await service.post_event("s-1", _new("e-2"))
        await self._until(lambda: self._closed(2))
        await service.close()
        first, second = await self.store.list_events(session_id="s-1")
        self.assertEqual((first.status, first.error["error_type"]), ("failed", "refused"))
        self.assertEqual(second.status, "completed")

    async def _closed(self, count: int) -> bool:
        return sum(s in ("completed", "failed", "cancelled") for s in await self._statuses()) == count

    async def test_an_event_waits_for_its_host_to_be_defined(self) -> None:
        pool = _Pool(self.requests)
        defined = {"assistant": False}
        pool.get_host = lambda host_id: host_id if defined.get(host_id) else (_ for _ in ()).throw(
            ValueError(host_id)
        )
        await self.store.insert_event(
            "s-1", "e-1", host_id="assistant", policy_id="p-1", body=_new("e-1").body()
        )
        service = EventService(pool, self.store)
        await service.start()
        self.assertEqual(await self._statuses(), ["queued"])
        defined["assistant"] = True
        service.host_defined("assistant")
        await self._until(lambda: self._done(1))
        await service.close()

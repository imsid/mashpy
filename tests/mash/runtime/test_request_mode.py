"""Request mode on the runtime: submit stores it, set_request_mode switches it."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from conftest import build_test_stores, seed_lifecycle
from mash.runtime import AgentRuntime
from mash.runtime.errors import RequestTerminalError
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.server import AgentServer
from mash.testing.runtime_fixtures import build_spec


class _Unowned:
    """Keeps the server's shutdown from clearing a store the test reads."""

    def __init__(self, store) -> None:
        self._store = store

    async def close(self) -> None:
        return None

    def __getattr__(self, name):
        return getattr(self._store, name)


class RequestModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        database = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        database.start()
        self.addCleanup(database.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    async def _runtime(self) -> AgentRuntime:
        runtime_store, memory_store = build_test_stores()
        runtime = AgentRuntime.from_spec(
            build_spec(agent_id="mode-app"),
            session_id="s-1",
            runtime_store=runtime_store,
            memory_store=memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def _seed_running(self, runtime: AgentRuntime, request_id: str) -> None:
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id=request_id,
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                event_type=RuntimeEventType.INVOKE_AGENT_START.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_START.value),
                dedupe_key="request.accepted",
                payload={"message": "hi", "mode": "fg", "started_by": "user"},
            )
        )

    async def test_submit_stores_the_mode_in_the_accepted_event_and_row(self) -> None:
        runtime = await self._runtime()
        accepted = await runtime.submit_request(
            message="hi", session_id="s-1", user_id="u-1", mode="bg"
        )
        events = await runtime.runtime_store.list_request_events(accepted["request_id"])
        self.assertEqual(events[0].payload["mode"], "bg")
        self.assertEqual(events[0].payload["started_by"], "user")
        self.assertIsNone(events[0].payload["parent_request_id"])
        [record] = await runtime.runtime_store.list_session_requests("s-1")
        self.assertEqual(record["mode"], "bg")

    async def test_submit_rejects_an_unknown_mode(self) -> None:
        runtime = await self._runtime()
        with self.assertRaises(ValueError):
            await runtime.submit_request(
                message="hi", session_id="s-1", user_id="u-1", mode="later"
            )

    async def test_set_request_mode_switches_both_ways(self) -> None:
        runtime = await self._runtime()
        await self._seed_running(runtime, "req-1")

        self.assertEqual(
            await runtime.set_request_mode("req-1", "bg"),
            {"request_id": "req-1", "agent_id": "mode-app", "mode": "bg"},
        )
        [record] = await runtime.runtime_store.list_session_requests("s-1")
        self.assertEqual(record["mode"], "bg")
        await runtime.set_request_mode("req-1", "fg")
        [record] = await runtime.runtime_store.list_session_requests("s-1")
        self.assertEqual(record["mode"], "fg")

        switches = [
            e.payload
            for e in await runtime.runtime_store.list_request_events("req-1")
            if e.event_type == RuntimeEventType.REQUEST_MODE_CHANGED.value
        ]
        self.assertEqual([s["mode"] for s in switches], ["bg", "fg"])

    async def test_set_request_mode_refuses_a_terminal_request(self) -> None:
        runtime = await self._runtime()
        await self._seed_running(runtime, "req-2")
        await runtime.runtime_store.append_event(
            RuntimeEvent(
                request_id="req-2",
                app_id=runtime.app_id,
                agent_id=runtime.app_id,
                session_id="s-1",
                event_type=RuntimeEventType.INVOKE_AGENT_END.value,
                lifecycle=seed_lifecycle(RuntimeEventType.INVOKE_AGENT_END.value, "ok"),
                dedupe_key="request.completed.0",
            )
        )
        with self.assertRaises(RequestTerminalError):
            await runtime.set_request_mode("req-2", "bg")
        [record] = await runtime.runtime_store.list_session_requests("s-1")
        self.assertEqual(record["mode"], "fg")

    async def test_set_request_mode_on_an_unknown_request_raises_key_error(self) -> None:
        runtime = await self._runtime()
        with self.assertRaises(KeyError):
            await runtime.set_request_mode("missing", "bg")


class RequestModeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        database = patch(
            "mash.runtime.service.resolve_database_url",
            return_value="postgresql://test/runtime",
        )
        database.start()
        self.addCleanup(database.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        runtime_store, memory_store = build_test_stores()
        self.store = runtime_store
        self.runtime = AgentRuntime.from_spec(
            build_spec(agent_id="mode-app"),
            session_id="s-1",
            runtime_store=_Unowned(runtime_store),
            memory_store=memory_store,
        )
        self.client = TestClient(AgentServer(self.runtime).app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _seed(self, *event_types: str) -> None:
        for event_type in event_types:
            self.client.portal.call(
                self.store.append_event,
                RuntimeEvent(
                    request_id="req-1",
                    app_id="mode-app",
                    agent_id="mode-app",
                    session_id="s-1",
                    event_type=event_type,
                    lifecycle=seed_lifecycle(event_type),
                    dedupe_key=event_type,
                ),
            )

    def _mode(self, mode, request_id: str = "req-1"):
        return self.client.post(
            f"/agent/mode-app/request/{request_id}/mode", json={"mode": mode}
        )

    def test_switches_both_ways_then_409_once_terminal(self) -> None:
        self._seed(RuntimeEventType.INVOKE_AGENT_START.value)
        for mode in ("bg", "fg"):
            resp = self._mode(mode)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["mode"], mode)

        self._seed(RuntimeEventType.INVOKE_AGENT_END.value)
        resp = self._mode("bg")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "REQUEST_TERMINAL")

    def test_unknown_request_is_404_and_bad_mode_is_400(self) -> None:
        self.assertEqual(self._mode("bg", request_id="missing").status_code, 404)
        self._seed(RuntimeEventType.INVOKE_AGENT_START.value)
        resp = self._mode("sideways")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")

    def test_other_agent_id_is_route_not_found(self) -> None:
        resp = self.client.post("/agent/other/request/req-1/mode", json={"mode": "bg"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "ROUTE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()

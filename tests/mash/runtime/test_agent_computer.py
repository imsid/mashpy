"""Agent computers: ``build_agent_server`` and the host's H2A relay.

The end-to-end test runs the host app and two agent servers as ASGI apps on
one in-memory "network": a primary on its own computer calls its subagent
through the host relay, which forwards to the subagent's computer. Each hop is
a real H2A HTTP call with the agent's token.
"""

from __future__ import annotations

import functools
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient

from conftest import build_test_stores
from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.api.routes.h2a import build_h2a_router
from mash.runtime import Host, HostBuilder
from mash.runtime.client import AgentClient
from mash.runtime.computer import HostRelay, build_agent_server
from mash.runtime.engine import dbos as engine_dbos
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.host.tokens import agent_for_token, derive_agent_token
from mash.testing.runtime_fixtures import build_delegating_spec, build_spec, metadata

SECRET = "host-secret"
HOST_SNAPSHOT = {"host_id": "assistant", "primary": "primary-app", "subagents": ["research"]}


def _build_pool():
    return (
        HostBuilder()
        .agent(
            build_delegating_spec(
                agent_id="primary-app",
                final_text="delegated-ok",
                subagent_id="research",
                subagent_prompt="analyze",
            ),
            metadata=metadata(),
        )
        .agent(build_spec(agent_id="research", response_text="research-ok"), metadata=metadata())
        .host(Host(host_id="assistant", primary="primary-app", subagents=("research",)))
        .build()
    )


class _Network(httpx.AsyncBaseTransport):
    """Routes each request to the ASGI app named by its URL host."""

    def __init__(self, apps: dict[str, Any]) -> None:
        self._transports = {name: httpx.ASGITransport(app=app) for name, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[request.url.host].handle_async_request(request)


class TokenTests(unittest.TestCase):
    def test_tokens_are_per_agent_and_resolve_back(self) -> None:
        primary = derive_agent_token(SECRET, "primary-app")

        self.assertNotEqual(primary, derive_agent_token(SECRET, "research"))
        self.assertNotEqual(primary, derive_agent_token("other-secret", "primary-app"))
        self.assertEqual(agent_for_token(SECRET, primary, ["research", "primary-app"]), "primary-app")
        self.assertIsNone(agent_for_token(SECRET, "forged", ["research", "primary-app"]))
        self.assertIsNone(agent_for_token(SECRET, "", ["primary-app"]))


class BuildAgentServerTests(unittest.TestCase):
    def setUp(self) -> None:
        state = patch.object(engine_dbos, "_STATE", engine_dbos._DBOSRuntimeState())
        state.start()
        self.addCleanup(state.stop)

    def test_configures_the_process_for_the_agent(self) -> None:
        server = build_agent_server(
            _build_pool(), "research", token="tok", host_api_url="http://host"
        )

        self.assertEqual(server.agent_id, "research")
        self.assertEqual(engine_dbos.get_executor_id(), "agent:research")
        self.assertFalse(engine_dbos._STATE.listen_queues)

    def test_subagents_resolve_to_the_host_relay(self) -> None:
        server = build_agent_server(
            _build_pool(), "primary-app", token="tok", host_api_url="http://host/"
        )

        relay = server.runtime.get_pool()
        self.assertIsInstance(relay, HostRelay)
        client = relay.get_client("research")
        self.assertEqual(client.base_url, "http://host/h2a")
        self.assertEqual(client._headers["Authorization"], "Bearer tok")
        self.assertIsNotNone(relay.get_agent_metadata("research"))

    def test_unknown_agent_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_agent_server(_build_pool(), "missing", token="tok", host_api_url="http://host")


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post_request(self, message: str, **kwargs: Any) -> str:
        self.calls.append({"message": message, **kwargs})
        return "req-sub"


class H2ARelayAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client_for_research = _RecordingClient()
        self.store, _ = build_test_stores()
        pool = SimpleNamespace(
            list_agents=lambda: ["primary-app", "research", "other"],
            get_client=lambda agent_id: self.client_for_research,
            get_runtime_store=lambda: self.store,
        )
        app = FastAPI()
        app.include_router(build_h2a_router())
        app.state.runtime_state = SimpleNamespace(pool=pool, host_secret=SECRET)
        self.app = app
        self.http = TestClient(app)

    def _submit(self, *, caller: str = "primary-app", **overrides: Any):
        body = {
            "message": "analyze",
            "session_id": "s-1",
            "request_id": "req-1-sub-0-0",
            "host": HOST_SNAPSHOT,
            "parent": {
                "kind": "subagent",
                "request_id": "req-1",
                "agent_id": "primary-app",
                "session_id": "s-1",
            },
        }
        body.update(overrides)
        return self.http.post(
            "/h2a/agent/research/request",
            json=body,
            headers={"Authorization": f"Bearer {derive_agent_token(SECRET, caller)}"},
        )

    def test_forwards_an_allowed_subagent_call(self) -> None:
        resp = self._submit()

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["request_id"], "req-sub")
        [call] = self.client_for_research.calls
        self.assertEqual(call["request_id"], "req-1-sub-0-0")
        self.assertEqual(call["host"], HOST_SNAPSHOT)
        self.assertEqual(call["parent"]["agent_id"], "primary-app")

    def test_invalid_token_is_401(self) -> None:
        resp = self.http.post(
            "/h2a/agent/research/request",
            json={"message": "analyze"},
            headers={"Authorization": "Bearer forged"},
        )

        self.assertEqual(resp.status_code, 401)

    def test_caller_must_be_the_parent_agent(self) -> None:
        resp = self._submit(caller="other")

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client_for_research.calls, [])

    def test_subagent_must_be_in_the_host_snapshot(self) -> None:
        resp = self._submit(host={**HOST_SNAPSHOT, "subagents": ["writer"]})

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client_for_research.calls, [])

    def test_workflow_parent_is_rejected(self) -> None:
        resp = self._submit(
            parent={"kind": "workflow", "workflow_id": "wf", "run_id": "r", "task_id": "t"}
        )

        self.assertEqual(resp.status_code, 403)

    def test_relay_is_unavailable_without_a_host_secret(self) -> None:
        self.app.state.runtime_state.host_secret = None

        self.assertEqual(self._submit().status_code, 503)

    def test_stream_is_only_for_the_submitting_agent(self) -> None:
        import asyncio

        asyncio.run(
            self.store.append_event(
                RuntimeEvent(
                    request_id="req-sub",
                    app_id="research",
                    agent_id="research",
                    session_id="s-1",
                    event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                    dedupe_key="request.accepted",
                    payload={"request_metadata": {"parent": {"agent_id": "primary-app"}}},
                )
            )
        )
        other = derive_agent_token(SECRET, "other")

        resp = self.http.get(
            "/h2a/agent/research/request/req-sub",
            headers={"Authorization": f"Bearer {other}"},
        )
        missing = self.http.get(
            "/h2a/agent/research/request/req-missing",
            headers={"Authorization": f"Bearer {other}"},
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(missing.status_code, 404)


class AgentComputerEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_calls_subagent_through_the_host_relay(self) -> None:
        state = patch.object(engine_dbos, "_STATE", engine_dbos._DBOSRuntimeState())
        state.start()
        self.addCleanup(state.stop)
        # The host app still builds masher's eval agents in-process, and their
        # LLM providers need a key.
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)
        # One shared Postgres: every process's stores see the same rows.
        runtime_store, memory_store = build_test_stores()
        for target, store in (
            ("mash.runtime.server.PostgresRuntimeStore", runtime_store),
            ("mash.runtime.host.host.PostgresRuntimeStore", runtime_store),
            ("mash.runtime.host.host.PostgresStore", memory_store),
            ("mash.runtime.spec.PostgresStore", memory_store),
        ):
            p = patch(target, lambda _url, _store=store: _store)
            p.start()
            self.addCleanup(p.stop)

        primary_token = derive_agent_token(SECRET, "primary-app")
        research_token = derive_agent_token(SECRET, "research")
        # Each computer imports the pool itself and serves one agent from it.
        primary_server = build_agent_server(
            _build_pool(), "primary-app", token=primary_token, host_api_url="http://host"
        )
        research_server = build_agent_server(
            _build_pool(), "research", token=research_token, host_api_url="http://host"
        )
        host_pool = _build_pool()
        host_app = create_app(
            host_pool,
            config=MashHostConfig(
                runtime_database_url="postgresql://test/runtime",
                host_secret=SECRET,
                api_logging_enabled=False,
            ),
        )
        network = _Network(
            {"host": host_app, "primary-app": primary_server.app, "research": research_server.app}
        )
        client_patch = patch(
            "mash.runtime.client.httpx.AsyncClient",
            functools.partial(httpx.AsyncClient, transport=network),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

        await primary_server.runtime.open()
        await research_server.runtime.open()
        try:
            async with host_app.router.lifespan_context(host_app):
                # Until the cutover, point the host's clients at the computers.
                host_pool._clients["primary-app"] = AgentClient(
                    "http://primary-app", "primary-app", token=primary_token
                )
                host_pool._clients["research"] = AgentClient(
                    "http://research", "research", token=research_token
                )

                accepted = await host_pool.submit_host_request(
                    "assistant", message="delegate", session_id="s-1"
                )
                events = [
                    event
                    async for event in host_pool.get_client("primary-app").stream_response(
                        accepted["request_id"], timeout=10
                    )
                ]
                # The subagent ran on its own computer, under the parent session.
                turns = await memory_store.get_turns(session_id="s-1", app_id="research")
        finally:
            await primary_server.runtime.shutdown()
            await research_server.runtime.shutdown()

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(events[-1]["data"]["response"]["text"], "delegated-ok")
        subagent_completed = [
            event
            for event in events
            if event["event"] == "agent.trace"
            and event["data"]["event_type"] == "subagent.request.completed"
        ]
        self.assertEqual(len(subagent_completed), 1)
        self.assertEqual(turns[-1]["user_message"], "analyze")


if __name__ == "__main__":
    unittest.main()

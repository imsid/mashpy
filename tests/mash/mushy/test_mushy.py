"""Summary capture and authenticated Thoughts API contracts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from mash.api import create_app, MashHostConfig
from mash.core.llm import LLMResponse
from mash.mushy import Mushy
from mash.mushy.catalog import EXPRESSIONS


def test_agent_spec_captures_summary_without_a_second_provider():
    from conftest import build_test_stores
    from mash.runtime import AgentRuntime
    from mash.testing.runtime_fixtures import build_spec

    async def run():
        spec = build_spec(agent_id="pilot", response_text="Answer")
        primary = spec.build_llm()
        primary.enable_thought_summaries = Mock()
        primary.send = AsyncMock(return_value=LLMResponse(
            thought_summary="I am considering alternatives.", text="Answer",
            tool_calls=[], content_blocks=[],
        ))
        spec.build_llm = lambda: primary
        spec.build_mushy = Mock(return_value=Mushy())
        runtime_store, memory_store = build_test_stores()
        runtime = AgentRuntime.from_spec(
            spec, session_id="session", runtime_store=runtime_store, memory_store=memory_store,
        )
        try:
            await runtime.open()
            accepted = await runtime.submit_request(message="Hello", session_id="session")
            cursor = 0
            for _ in range(100):
                _, cursor, done = await runtime.stream_response_events(
                    accepted["request_id"], cursor=cursor, wait_timeout=0.1
                )
                if done:
                    break
            assert done
            events = await runtime_store.list_request_events(accepted["request_id"])
            thoughts = [e for e in events if e.event_type == "runtime.llm.think.completed"]
            assert thoughts[0].payload["thought_summary"] == "I am considering alternatives."
            primary.enable_thought_summaries.assert_called()
            spec.build_mushy.assert_called_once()
        finally:
            await runtime.shutdown()
    asyncio.run(run())


def api_client():
    store = SimpleNamespace(list_thought_traces=AsyncMock(return_value=[{
        "trace_id": "trace", "agent_id": "pilot", "session_id": "session",
        "latest_thought_at": 10, "summaries": [{"event_id": 1,
        "thought_summary": "**Investigating**\n\nI'm now diving deep into potential pitfalls.  \n"}],
    }]))
    def agent(agent_id):
        if agent_id != "pilot":
            raise ValueError("Agent not found")
        return SimpleNamespace(runtime_store=store)
    pool = SimpleNamespace(get_runtime_store=lambda: store, get_agent=agent)
    app = create_app(pool, config=MashHostConfig(api_logging_enabled=False))
    app.state.runtime_state = SimpleNamespace(pool=pool, api_key="secret")
    return TestClient(app), store


def test_feed_auth_filter_raw_text_and_fixed_trace_limit():
    client, store = api_client()
    path = "/api/v1/telemetry/thoughts"
    assert client.get(path).status_code == 401
    store.list_thought_traces.assert_not_called()
    auth = {"Authorization": "Bearer secret"}
    response = client.get(path, params={"agent_id": "pilot", "limit": 9000}, headers=auth)
    assert response.status_code == 200
    assert response.json()["data"]["limit"] == 50
    assert response.json()["data"]["traces"] == store.list_thought_traces.return_value
    store.list_thought_traces.assert_awaited_once_with("pilot", limit=50)
    client.get(path, headers=auth)
    store.list_thought_traces.assert_awaited_with(None, limit=50)
    assert client.get(path, params={"agent_id": "missing"}, headers=auth).status_code == 404
    client.cookies.set("mash_api_key", "secret")
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("definition", EXPRESSIONS.values())
@pytest.mark.parametrize("format", ["gif", "png"])
def test_expression_assets_are_authenticated_and_catalog_backed(definition, format):
    client, _ = api_client()
    path = "/api/v1/telemetry/thoughts/expressions/" + definition.asset.replace('.gif', '.' + format)
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/" + format
    assert response.content.startswith(b'GIF' if format == 'gif' else b'\x89PNG')
    assert "private" in response.headers["cache-control"]


def test_old_gif_generation_and_public_share_routes_are_gone():
    client, _ = api_client()
    for path in ("/mushy/00000000-0000-0000-0000-000000000001", "/mushy/00000000-0000-0000-0000-000000000001.gif"):
        assert client.get(path).status_code == 404
    assert client.post("/api/v1/agent/pilot/request/request/mushy", headers={"Authorization": "Bearer secret"}).status_code == 404
    assert client.get("/api/v1/telemetry/thoughts/expressions/unknown.gif", headers={"Authorization": "Bearer secret"}).status_code == 404

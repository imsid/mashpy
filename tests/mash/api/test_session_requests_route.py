"""The session request list over the host API."""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import (
    RoutingLLMProvider,
    build_spec,
    metadata,
    patch_companion_llm,
)


@contextmanager
def _client():
    # The companion's classifier is the slow part: it routes every event to "fast".
    slow = RoutingLLMProvider("fast", delay_seconds=0.6)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ), patch_companion_llm(slow):
        pool = (
            HostBuilder()
            .agent(build_spec(agent_id="fast", response_text="done"), metadata=metadata())
            .host(Host(host_id="assistant", agents=("fast",)))
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def _send(client: TestClient, event_id: str, message: str) -> str:
    resp = client.post(
        "/api/v1/sessions/s-1/events",
        json={
            "event_id": event_id,
            "host_id": "assistant",
            "app_id": "app-1",
            "policy_id": "p-1",
            "message": message,
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]["request_id"]


def _list(client: TestClient, agent_id: str = "companion", **params) -> list[dict]:
    resp = client.get(f"/api/v1/agent/{agent_id}/sessions/s-1/requests", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["requests"]


def _wait_for_status(client: TestClient, request_id: str, status: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(r["request_id"] == request_id and r["status"] == status for r in _list(client)):
            return
        time.sleep(0.05)
    raise AssertionError(f"{request_id} never reached {status}")


def test_session_list_filters_and_orders_newest_first() -> None:
    with _client() as client:
        first = _send(client, "e-1", "first")
        second = _send(client, "e-2", "second")
        _wait_for_status(client, first, "completed")
        _wait_for_status(client, second, "running")

        everything = _list(client)
        # The session holds the companion's requests and the one each routed.
        [routed] = [r for r in everything if r["agent_id"] == "fast"]
        assert routed["parent_request_id"] == first
        assert routed["started_by"] == "companion"
        assert [r["request_id"] for r in everything] == [second, routed["request_id"], first]
        assert everything[0] == {
            "request_id": second,
            "agent_id": "companion",
            "app_id": "app-1",
            "policy_id": "p-1",
            "message": "second",
            "status": "running",
            "started_by": "user",
            "parent_request_id": None,
            "started_at": everything[0]["started_at"],
        }
        assert isinstance(everything[0]["started_at"], float)

        assert [r["request_id"] for r in _list(client, status="running")] == [second]
        assert [r["request_id"] for r in _list(client, started_by="user")] == [second, first]
        assert [r["request_id"] for r in _list(client, started_by="companion")] == [
            routed["request_id"]
        ]
        assert _list(client, started_by="planner") == []
        _wait_for_status(client, second, "completed")


def test_session_list_refuses_an_unknown_status() -> None:
    with _client() as client:
        resp = client.get("/api/v1/agent/fast/sessions/s-1/requests", params={"status": "later"})
        assert resp.status_code == 422


def test_session_list_on_an_unknown_agent_is_404() -> None:
    with _client() as client:
        resp = client.get("/api/v1/agent/ghost/sessions/s-1/requests")
        assert resp.status_code == 404

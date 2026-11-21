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
from mash.runtime import HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


@contextmanager
def _client():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ):
        pool = (
            HostBuilder()
            .agent(build_spec(agent_id="fast", response_text="done"), metadata=metadata())
            .agent(
                build_spec(agent_id="slow", response_text="late", delay_seconds=1.5),
                metadata=metadata(),
            )
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def _submit(client: TestClient, agent_id: str, **body) -> str:
    resp = client.post(
        f"/api/v1/agent/{agent_id}/request",
        json={"session_id": "s-1", "app_id": "app-1", "policy_id": "p-1", **body},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["request_id"]


def _list(client: TestClient, agent_id: str = "fast", **params) -> list[dict]:
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
        first = _submit(client, "fast", message="first")
        _wait_for_status(client, first, "completed")
        second = _submit(client, "slow", message="second")
        third = _submit(client, "fast", message="third")
        _wait_for_status(client, third, "completed")

        everything = _list(client)
        assert [r["request_id"] for r in everything] == [third, second, first]
        assert everything[1] == {
            "request_id": second,
            "agent_id": "slow",
            "app_id": "app-1",
            "policy_id": "p-1",
            "message": "second",
            "status": "running",
            "started_by": "user",
            "parent_request_id": None,
            "started_at": everything[1]["started_at"],
        }
        assert isinstance(everything[1]["started_at"], float)

        assert [r["request_id"] for r in _list(client, status="running")] == [second]
        assert [r["request_id"] for r in _list(client, started_by="user")] == [third, second, first]
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

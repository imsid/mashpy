"""Request mode over the host API: submit, switch, and the session list."""

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
            .host(Host(host_id="assistant", primary="fast"))
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
        json={"session_id": "s-1", "user_id": "u-1", **body},
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


def test_submit_records_the_mode() -> None:
    with _client() as client:
        background = _submit(client, "fast", message="in the back", mode="bg")
        foreground = _submit(client, "fast", message="in the front")
        _wait_for_status(client, background, "completed")
        _wait_for_status(client, foreground, "completed")

        modes = {r["request_id"]: r["mode"] for r in _list(client)}
        assert modes == {background: "bg", foreground: "fg"}


def test_host_submit_takes_the_mode() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/hosts/assistant/request",
            json={"message": "hi", "session_id": "s-1", "user_id": "u-1", "mode": "bg"},
        )
        assert resp.status_code == 200, resp.text
        request_id = resp.json()["data"]["request_id"]
        [record] = _list(client, mode="bg")
        assert record["request_id"] == request_id


def test_an_unknown_mode_fails_validation() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/agent/fast/request",
            json={"message": "hi", "session_id": "s-1", "user_id": "u-1", "mode": "later"},
        )
        assert resp.status_code == 422
        resp = client.get("/api/v1/agent/fast/sessions/s-1/requests", params={"mode": "later"})
        assert resp.status_code == 422


def test_mode_switches_both_ways_while_running_and_409_once_done() -> None:
    with _client() as client:
        request_id = _submit(client, "slow", message="take your time")

        to_bg = client.post(f"/api/v1/agent/slow/request/{request_id}/mode", json={"mode": "bg"})
        assert to_bg.status_code == 200, to_bg.text
        assert to_bg.json()["data"] == {"request_id": request_id, "agent_id": "slow", "mode": "bg"}
        assert _list(client, mode="bg")[0]["request_id"] == request_id

        to_fg = client.post(f"/api/v1/agent/slow/request/{request_id}/mode", json={"mode": "fg"})
        assert to_fg.status_code == 200, to_fg.text
        assert _list(client, mode="bg") == []

        _wait_for_status(client, request_id, "completed")
        done = client.post(f"/api/v1/agent/slow/request/{request_id}/mode", json={"mode": "bg"})
        assert done.status_code == 409
        assert done.json()["error"]["code"] == "REQUEST_TERMINAL"
        assert _list(client)[0]["mode"] == "fg"


def test_mode_on_an_unknown_request_is_404() -> None:
    with _client() as client:
        resp = client.post("/api/v1/agent/fast/request/nope/mode", json={"mode": "bg"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "REQUEST_NOT_FOUND"


def test_session_list_filters_and_orders_newest_first() -> None:
    with _client() as client:
        first = _submit(client, "fast", message="first", mode="bg")
        _wait_for_status(client, first, "completed")
        second = _submit(client, "slow", message="second", mode="bg")
        third = _submit(client, "fast", message="third")
        _wait_for_status(client, third, "completed")

        everything = _list(client)
        assert [r["request_id"] for r in everything] == [third, second, first]
        assert everything[1] == {
            "request_id": second,
            "agent_id": "slow",
            "message": "second",
            "mode": "bg",
            "status": "running",
            "started_by": "user",
            "parent_request_id": None,
            "started_at": everything[1]["started_at"],
        }
        assert isinstance(everything[1]["started_at"], float)

        assert [r["request_id"] for r in _list(client, mode="bg")] == [second, first]
        assert [r["request_id"] for r in _list(client, mode="bg", status="running")] == [second]
        assert [r["request_id"] for r in _list(client, started_by="user")] == [third, second, first]
        assert _list(client, started_by="planner") == []
        _wait_for_status(client, second, "completed")


def test_session_list_on_an_unknown_agent_is_404() -> None:
    with _client() as client:
        resp = client.get("/api/v1/agent/ghost/sessions/s-1/requests")
        assert resp.status_code == 404

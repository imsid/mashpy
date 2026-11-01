"""Cancelling a request on the host also cancels its running children."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from conftest import seed_lifecycle
from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import HostBuilder
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import build_spec, metadata


@contextmanager
def _client():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ):
        pool = (
            HostBuilder()
            .agent(build_spec(agent_id="primary"), metadata=metadata())
            .agent(build_spec(agent_id="research"), metadata=metadata())
            .agent(build_spec(agent_id="solo"), metadata=metadata())
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def _store(client: TestClient):
    return client.app.state.runtime_state.pool.get_runtime_store()


def _append(client: TestClient, request_id: str, agent_id: str, event_type: str, **payload):
    client.portal.call(
        _store(client).append_event,
        RuntimeEvent(
            request_id=request_id,
            app_id=agent_id,
            agent_id=agent_id,
            session_id="s-1",
            trace_id=f"trace-{request_id}",
            event_type=event_type,
            lifecycle=seed_lifecycle(event_type, payload.get("status")),
            dedupe_key=event_type,
            payload=payload,
        ),
    )


def _accept(client: TestClient, request_id: str, agent_id: str, parent: str | None = None):
    _append(
        client,
        request_id,
        agent_id,
        RuntimeEventType.INVOKE_AGENT_START.value,
        message=request_id,
        mode="bg" if parent else "fg",
        started_by="primary" if parent else "user",
        parent_request_id=parent,
    )


def _statuses(client: TestClient) -> dict[str, str]:
    records = client.portal.call(_store(client).list_session_requests, "s-1")
    return {r["request_id"]: r["status"] for r in records}


def _seed_family(client: TestClient) -> None:
    _accept(client, "parent", "primary")
    _accept(client, "child-a", "research", parent="parent")
    _accept(client, "child-b", "primary", parent="parent")
    _accept(client, "child-done", "research", parent="parent")
    _append(client, "child-done", "research", RuntimeEventType.INVOKE_AGENT_END.value, status="ok")
    _accept(client, "stranger", "research", parent="someone-else")


def test_cancelling_a_parent_cancels_its_running_children() -> None:
    with _client() as client:
        _seed_family(client)

        resp = client.post("/api/v1/agent/primary/request/parent/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "cancelled"
        assert _statuses(client) == {
            "parent": "cancelled",
            "child-a": "cancelled",
            "child-b": "cancelled",
            "child-done": "completed",
            "stranger": "running",
        }


def test_the_trace_cancel_route_also_cancels_children() -> None:
    with _client() as client:
        _seed_family(client)

        resp = client.post("/api/v1/agent/primary/trace/trace-parent/cancel")
        assert resp.status_code == 200, resp.text
        statuses = _statuses(client)
        assert statuses["child-a"] == statuses["child-b"] == "cancelled"
        assert statuses["child-done"] == "completed"


def test_a_child_on_a_disabled_agent_is_skipped() -> None:
    with _client() as client:
        _accept(client, "parent", "primary")
        _accept(client, "child-solo", "solo", parent="parent")
        _accept(client, "child-a", "research", parent="parent")
        assert client.post("/api/v1/agent/solo/disable").status_code == 200

        resp = client.post("/api/v1/agent/primary/request/parent/cancel")
        assert resp.status_code == 200, resp.text
        statuses = _statuses(client)
        assert statuses["parent"] == "cancelled"
        assert statuses["child-a"] == "cancelled"
        assert statuses["child-solo"] == "running"

"""Enabling and disabling agents on a running host."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import Host, HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import AgentStep, WorkflowSpec


@contextmanager
def _client():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ):
        pool = (
            HostBuilder()
            .agent(build_spec(agent_id="primary", response_text="p"), metadata=metadata())
            .agent(build_spec(agent_id="research", response_text="r"), metadata=metadata())
            .agent(build_spec(agent_id="solo", response_text="solo-ok"), metadata=metadata())
            .agent(build_spec(agent_id="worker", response_text="{}"), metadata=metadata())
            .host(Host(host_id="assistant", primary="primary", subagents=("research",)))
            .workflow(
                WorkflowSpec(
                    workflow_id="wf",
                    steps=[AgentStep(step_id="s", agent_id="worker", output={"type": "object"})],
                )
            )
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def _agents(client: TestClient) -> dict[str, dict]:
    return {a["agent_id"]: a for a in client.get("/api/v1/agent").json()["data"]["agents"]}


def test_every_agent_starts_enabled_and_running() -> None:
    with _client() as client:
        agents = _agents(client)
        assert {a["enabled"] for a in agents.values()} == {True}
        assert {a["status"] for a in agents.values()} == {"running"}


def test_disable_is_refused_while_a_host_or_workflow_names_the_agent() -> None:
    with _client() as client:
        for agent_id, reference in (("research", "host 'assistant'"), ("worker", "workflow 'wf'")):
            resp = client.post(f"/api/v1/agent/{agent_id}/disable")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "AGENT_IN_USE"
            assert resp.json()["error"]["details"]["references"] == [reference]
        assert _agents(client)["research"]["enabled"] is True


def test_a_disabled_agent_takes_no_requests_until_enabled() -> None:
    with _client() as client:
        disabled = client.post("/api/v1/agent/solo/disable")
        assert disabled.status_code == 200
        assert disabled.json()["data"]["agent"] | {"metadata": None} == {
            "agent_id": "solo",
            "metadata": None,
            "enabled": False,
            "status": "disabled",
        }

        body = {"message": "hi", "session_id": "s-1", "user_id": "u-1"}
        submitted = client.post("/api/v1/agent/solo/request", json=body)
        assert submitted.status_code == 409
        assert submitted.json()["error"]["code"] == "AGENT_DISABLED"
        assert client.get("/api/v1/agent/solo").json()["data"]["session"] is None
        host = client.put("/api/v1/hosts/solo-host", json={"primary": "solo"})
        assert host.status_code == 400

        enabled = client.post("/api/v1/agent/solo/enable")
        assert enabled.json()["data"]["agent"]["status"] == "running"
        submitted = client.post("/api/v1/agent/solo/request", json=body)
        assert submitted.status_code == 200


def test_unknown_agent_is_404() -> None:
    with _client() as client:
        assert client.post("/api/v1/agent/missing/disable").status_code == 404
        assert client.post("/api/v1/agent/missing/enable").status_code == 404

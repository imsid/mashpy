"""A session belongs to the user of its first request."""

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


@contextmanager
def _client():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ):
        pool = (
            HostBuilder()
            .agent(build_spec(agent_id="fast", response_text="done"), metadata=metadata())
            .agent(build_spec(agent_id="other", response_text="done"), metadata=metadata())
            .host(Host(host_id="assistant", primary="fast"))
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def _submit(client: TestClient, path: str, *, user_id: str, session_id: str = "s-1"):
    return client.post(
        path, json={"message": "hi", "session_id": session_id, "user_id": user_id}
    )


def test_another_users_request_on_a_session_is_409() -> None:
    with _client() as client:
        assert _submit(client, "/api/v1/agent/fast/request", user_id="u-1").status_code == 200

        resp = _submit(client, "/api/v1/agent/fast/request", user_id="u-2")

        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "SESSION_USER_MISMATCH"


def test_the_session_is_the_users_across_agents_and_hosts() -> None:
    with _client() as client:
        assert _submit(client, "/api/v1/agent/fast/request", user_id="u-1").status_code == 200

        on_other_agent = _submit(client, "/api/v1/agent/other/request", user_id="u-2")
        on_host = _submit(client, "/api/v1/hosts/assistant/request", user_id="u-2")

        assert on_other_agent.status_code == 409, on_other_agent.text
        assert on_host.status_code == 409, on_host.text
        assert on_host.json()["error"]["code"] == "SESSION_USER_MISMATCH"


def test_the_same_user_and_other_sessions_still_submit() -> None:
    with _client() as client:
        assert _submit(client, "/api/v1/agent/fast/request", user_id="u-1").status_code == 200

        assert _submit(client, "/api/v1/agent/fast/request", user_id="u-1").status_code == 200
        assert _submit(client, "/api/v1/hosts/assistant/request", user_id="u-1").status_code == 200
        assert (
            _submit(client, "/api/v1/agent/fast/request", user_id="u-2", session_id="s-2").status_code
            == 200
        )

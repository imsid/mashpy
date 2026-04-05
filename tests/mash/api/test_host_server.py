"""Integration tests for the public host API over hosted runtimes."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import MashHostConfig, create_app
from mash.api.telemetry_ui import TELEMETRY_API_KEY_COOKIE, get_telemetry_static_dir
from mash.runtime import MashAgentHostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


@contextmanager
def _build_test_client(root: Path, *, api_key: str | None = None):
    with patch.dict(os.environ, {"MASH_DATA_DIR": str(root)}):
        host = (
            MashAgentHostBuilder()
            .primary(build_spec(agent_id="primary", response_text="primary-ok"))
            .subagent(
                build_spec(
                    agent_id="research",
                    response_text="research-ok",
                ),
                metadata=metadata(),
            )
            .build()
        )
        app = create_app(
            host,
            config=MashHostConfig(
                api_key=api_key,
                observability_memory_db_path=root / "memory.db",
            ),
        )
        with TestClient(app) as client:
            yield client


def test_health_and_agent_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            telemetry = client.get("/telemetry")
            assert telemetry.status_code == 200
            assert "text/html" in telemetry.headers["content-type"]

            health = client.get("/api/v1/health")
            assert health.status_code == 200
            payload = health.json()["data"]
            assert payload["service"] == "mash-api"
            assert payload["deployment"]["primary_agent_id"] == "primary"
            assert len(payload["deployment"]["agents"]) == 2
            assert payload["primary_agent"]["agent_id"] == "primary"

            agents = client.get("/api/v1/agent")
            assert agents.status_code == 200
            assert len(agents.json()["data"]["agents"]) == 2

            static_dir = get_telemetry_static_dir()
            asset_paths = sorted(
                path.relative_to(static_dir).as_posix()
                for path in (static_dir / "assets").iterdir()
            )
            assert asset_paths

            asset = client.get(f"/telemetry/{asset_paths[0]}")
            assert asset.status_code == 200

            spa = client.get("/telemetry/sessions/foo")
            assert spa.status_code == 200
            assert "text/html" in spa.headers["content-type"]


def test_agent_scoped_invoke_and_session_routes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            invoke = client.post(
                "/api/v1/agent/primary/invoke",
                json={"message": "hello", "session_id": "s1"},
            )
            assert invoke.status_code == 200
            payload = invoke.json()["data"]
            assert payload["response"]["text"] == "primary-ok"

            sessions = client.get("/api/v1/agent/primary/sessions")
            assert sessions.status_code == 200
            assert sessions.json()["data"]["sessions"][0]["session_id"] == "s1"

            history = client.get("/api/v1/agent/primary/sessions/s1/history")
            assert history.status_code == 200
            assert len(history.json()["data"]["turns"]) == 1


def test_async_request_stream_and_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret") as client:
            unauthorized = client.get("/api/v1/health")
            assert unauthorized.status_code == 401

            headers = {"Authorization": "Bearer secret"}
            submitted = client.post(
                "/api/v1/agent/research/request",
                json={"message": "hello", "session_id": "s1"},
                headers=headers,
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]

            with client.stream(
                "GET",
                f"/api/v1/agent/research/request/{request_id}/events",
                headers=headers,
            ) as stream:
                assert stream.status_code == 200
                names: list[str] = []
                current_event = None
                for line in stream.iter_lines():
                    if not line:
                        continue
                    text = line if isinstance(line, str) else line.decode("utf-8")
                    if text.startswith("event:"):
                        current_event = text.split(":", 1)[1].strip()
                    if text.startswith("data:") and current_event:
                        names.append(current_event)
                        if current_event in {"request.completed", "request.error"}:
                            break
                assert "request.accepted" in names
                assert names[-1] == "request.completed"


def test_same_session_overlap_relays_waiting_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"MASH_DATA_DIR": str(root)}):
            host = (
                MashAgentHostBuilder()
                .primary(
                    build_spec(
                        agent_id="primary",
                        response_text="primary-ok",
                        delay_seconds=0.25,
                    )
                )
                .build()
            )
            app = create_app(
                host,
                config=MashHostConfig(
                    observability_memory_db_path=root / "memory.db",
                ),
            )
            with TestClient(app) as client:
                first = client.post(
                    "/api/v1/agent/primary/request",
                    json={"message": "first", "session_id": "shared"},
                )
                assert first.status_code == 200
                second = client.post(
                    "/api/v1/agent/primary/request",
                    json={"message": "second", "session_id": "shared"},
                )
                assert second.status_code == 200
                request_id = second.json()["data"]["request_id"]

                with client.stream(
                    "GET",
                    f"/api/v1/agent/primary/request/{request_id}/events",
                ) as stream:
                    assert stream.status_code == 200
                    names: list[str] = []
                    current_event = None
                    for line in stream.iter_lines():
                        if not line:
                            continue
                        text = line if isinstance(line, str) else line.decode("utf-8")
                        if text.startswith("event:"):
                            current_event = text.split(":", 1)[1].strip()
                        if text.startswith("data:") and current_event:
                            names.append(current_event)
                            if current_event in {"request.completed", "request.error"}:
                                break

                    assert names[0] == "request.accepted"
                    assert "request.waiting" in names
                    assert names[-1] == "request.completed"


def test_telemetry_ui_bootstraps_auth_cookie() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret") as client:
            unauthorized = client.get("/api/v1/health")
            assert unauthorized.status_code == 401

            telemetry = client.get("/telemetry")
            assert telemetry.status_code == 200
            assert client.cookies.get(TELEMETRY_API_KEY_COOKIE) == "secret"

            authorized = client.get("/api/v1/health")
            assert authorized.status_code == 200


def test_telemetry_events_filter_by_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            invoke = client.post(
                "/api/v1/agent/primary/invoke",
                json={"agent_id": "primary", "message": "hello", "session_id": "s-1"},
            )
            assert invoke.status_code == 200
            events = client.get("/api/v1/telemetry/events?agent_id=primary")
            assert events.status_code == 200
            payload = events.json()["data"]
            assert any(event["event_type"] == "agent.run.start" for event in payload["events"])
            assert payload["path"].endswith("/primary/state.db")


def test_missing_telemetry_assets_fail_fast() -> None:
    with patch("mash.api.app.mount_telemetry_ui", side_effect=RuntimeError("missing telemetry assets")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": str(root)}):
                host = MashAgentHostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                try:
                    create_app(host, config=MashHostConfig())
                except RuntimeError as exc:
                    assert "missing telemetry assets" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected create_app() to fail when telemetry assets are missing")

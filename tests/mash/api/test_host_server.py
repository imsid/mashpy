"""Integration tests for the public host API over hosted runtimes."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import MashHostConfig, create_app
from mash.api.telemetry_ui import TELEMETRY_API_KEY_COOKIE, get_telemetry_static_dir
from mash.runtime import HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


@contextmanager
def _build_test_client(root: Path, *, api_key: str | None = None):
    with patch.dict(
        os.environ,
        {
            "MASH_DATA_DIR": str(root),
            "MASH_MEMORY_DATABASE_URL": "",
        },
    ):
        host = (
            HostBuilder()
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
            assert payload["observability"]["memory"]["search_available"] is True

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


def test_agent_scoped_request_and_session_routes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s1"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            payload = _collect_terminal_response(client, "primary", request_id)
            assert payload["response"]["text"] == "primary-ok"

            sessions = client.get("/api/v1/agent/primary/sessions")
            assert sessions.status_code == 200
            assert sessions.json()["data"]["sessions"][0]["session_id"] == "s1"

            history = client.get("/api/v1/agent/primary/sessions/s1/history")
            assert history.status_code == 200
            assert len(history.json()["data"]["turns"]) == 1


def test_invoke_route_is_removed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.post(
                "/api/v1/agent/primary/invoke",
                json={"message": "hello", "session_id": "s1"},
            )
            assert response.status_code == 404


def test_removed_session_state_routes_return_not_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            assert (
                client.get("/api/v1/agent/primary/sessions/s1/preferences").status_code
                == 404
            )
            assert (
                client.put(
                    "/api/v1/agent/primary/sessions/s1/preferences",
                    json={"preferences": {"tone": "brief"}},
                ).status_code
                == 404
            )
            assert (
                client.get("/api/v1/agent/primary/sessions/s1/app-data").status_code
                == 404
            )
            assert (
                client.get("/api/v1/agent/primary/sessions/s1/app-data/key").status_code
                == 404
            )
            assert (
                client.put(
                    "/api/v1/agent/primary/sessions/s1/app-data/key",
                    json={"value": "x"},
                ).status_code
                == 404
            )
            assert (
                client.delete("/api/v1/agent/primary/sessions/s1/app-data/key").status_code
                == 404
            )


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


def test_same_session_overlap_completes_without_waiting_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": str(root),
                "MASH_MEMORY_DATABASE_URL": "",
            },
        ):
            host = (
                HostBuilder()
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
                config=MashHostConfig(),
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
                    assert "request.waiting" not in names
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
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s-1"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            payload = _collect_terminal_response(client, "primary", request_id)
            assert payload["response"]["text"] == "primary-ok"
            events = client.get("/api/v1/telemetry/events?agent_id=primary")
            assert events.status_code == 200
            payload = events.json()["data"]
            assert any(event["event_type"] == "agent.run.start" for event in payload["events"])
            assert payload["source"] == "runtime_event_log"


def test_reasoning_trace_route_returns_compact_trace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s-1"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            terminal = _collect_terminal_response(client, "primary", request_id)
            trace_id = terminal["trace_id"]

            reasoning_trace = client.get(
                "/api/v1/telemetry/reasoning-trace",
                params={
                    "agent_id": "primary",
                    "session_id": "s-1",
                    "trace_id": trace_id,
                },
            )
            assert reasoning_trace.status_code == 200
            payload = reasoning_trace.json()["data"]
            assert payload["source"] == "runtime_event_log"
            assert payload["agent_id"] == "primary"
            assert payload["session_id"] == "s-1"
            assert payload["trace_id"] == trace_id
            assert payload["status"] == "completed"
            assert payload["steps"]
            assert payload["summary"]["total_steps"] >= 1
            assert payload["steps"][0]["title"]


def test_reasoning_trace_route_rejects_blank_scope_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get(
                "/api/v1/telemetry/reasoning-trace",
                params={
                    "agent_id": "primary",
                    "session_id": " ",
                    "trace_id": "trace-1",
                },
            )
            assert response.status_code == 400
            payload = response.json()["error"]
            assert payload["code"] == "INVALID_REQUEST"
            assert payload["message"] == "session_id is required"


def test_memory_search_uses_agent_memory_store_by_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello world", "session_id": "s-1"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            payload = _collect_terminal_response(client, "primary", request_id)
            assert payload["response"]["text"] == "primary-ok"

            search = client.get(
                "/api/v1/telemetry/memory/search",
                params={
                    "q": "@user: hello",
                    "app_id": "primary",
                    "session_id": "s-1",
                },
            )
            assert search.status_code == 200
            payload = search.json()["data"]
            assert payload["app_id"] == "primary"
            assert payload["session_id"] == "s-1"
            assert payload["results"]
            assert payload["results"][0]["preview"]


def test_missing_telemetry_assets_fail_fast() -> None:
    with patch("mash.api.app.mount_telemetry_ui", side_effect=RuntimeError("missing telemetry assets")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "MASH_DATA_DIR": str(root),
                    "MASH_MEMORY_DATABASE_URL": "",
                },
            ):
                host = HostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                try:
                    create_app(host, config=MashHostConfig())
                except RuntimeError as exc:
                    assert "missing telemetry assets" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected create_app() to fail when telemetry assets are missing")


def _collect_terminal_response(client: TestClient, agent_id: str, request_id: str) -> dict[str, object]:
    with client.stream(
        "GET",
        f"/api/v1/agent/{agent_id}/request/{request_id}/events",
    ) as stream:
        assert stream.status_code == 200
        current_event = None
        for line in stream.iter_lines():
            if not line:
                continue
            text = line if isinstance(line, str) else line.decode("utf-8")
            if text.startswith("event:"):
                current_event = text.split(":", 1)[1].strip()
                continue
            if not text.startswith("data:") or current_event is None:
                continue
            if current_event == "request.completed":
                payload = json.loads(text.split(":", 1)[1].strip())
                assert isinstance(payload, dict)
                return payload
            if current_event == "request.error":
                raise AssertionError(f"request failed: {text}")
    raise AssertionError("stream ended without request.completed")

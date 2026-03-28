"""Integration tests for Mash host server composition."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.core.config import AgentConfig
from mash.core.context import Context, Response
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.runtime import AgentSpec, MashAgentHostBuilder, SubAgentMetadata
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry
from mash.api import MashHostConfig, create_app
from mash.api.telemetry_ui import TELEMETRY_API_KEY_COOKIE, get_telemetry_static_dir


class _FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    def send(self, request: LLMRequest) -> LLMResponse:
        del request
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _Spec(AgentSpec):
    def __init__(self, root: Path, *, agent_id: str) -> None:
        self.root = root
        self.agent_id = agent_id

    def get_agent_id(self) -> str:
        return self.agent_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.agent_id, system_prompt=f"You are {self.agent_id}")


@contextmanager
def _build_test_client(root: Path, *, api_key: str | None = None):
    with patch.dict(os.environ, {"MASH_DATA_DIR": str(root)}):
        host = (
            MashAgentHostBuilder()
            .primary(_Spec(root, agent_id="primary"), agent_id="primary")
            .subagent(
                _Spec(root, agent_id="research"),
                agent_id="research",
                metadata=SubAgentMetadata(
                    display_name="Research",
                    description="Research specialist",
                    capabilities=["search"],
                    usage_guidance="Use for research tasks.",
                ),
            )
            .build()
        )
        app = create_app(host, config=MashHostConfig(api_key=api_key, observability_memory_db_path=root / "memory.db"))
        with TestClient(app) as client:
            yield client


def _response(text: str = "ok") -> Response:
    return Response(text=text, context=Context(), metadata={"token_usage": {"input": 2, "output": 1}})


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

            agents = client.get("/api/v1/agents")
            assert agents.status_code == 200
            assert len(agents.json()["data"]["agents"]) == 2

            static_dir = get_telemetry_static_dir()
            asset_paths = sorted(path.relative_to(static_dir).as_posix() for path in (static_dir / "assets").iterdir())
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
            runtime = client.app.state.runtime_state.host.get_agent("primary")
            with patch.object(runtime.agent, "run", return_value=_response("primary-ok")):
                invoke = client.post(
                    "/api/v1/agents/primary/invoke",
                    json={"message": "hello", "session_id": "s1"},
                )
                assert invoke.status_code == 200
                payload = invoke.json()["data"]
                assert payload["response"]["text"] == "primary-ok"

            sessions = client.get("/api/v1/agents/primary/sessions")
            assert sessions.status_code == 200
            assert sessions.json()["data"]["sessions"][0]["session_id"] == "s1"

            history = client.get("/api/v1/agents/primary/sessions/s1/history")
            assert history.status_code == 200
            assert len(history.json()["data"]["turns"]) == 1


def test_async_request_stream_and_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret") as client:
            unauthorized = client.get("/api/v1/health")
            assert unauthorized.status_code == 401

            headers = {"Authorization": "Bearer secret"}
            runtime = client.app.state.runtime_state.host.get_agent("research")
            with patch.object(runtime.agent, "run", return_value=_response("research-ok")):
                submitted = client.post(
                    "/api/v1/agents/research/requests",
                    json={"message": "hello", "session_id": "s1"},
                    headers=headers,
                )
                assert submitted.status_code == 200
                request_id = submitted.json()["data"]["request_id"]

                with client.stream(
                    "GET",
                    f"/api/v1/agents/research/requests/{request_id}/events",
                    headers=headers,
                ) as stream:
                    assert stream.status_code == 200
                    names: list[str] = []
                    current_event: Optional[str] = None
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
            runtime = client.app.state.runtime_state.host.get_agent("primary")
            runtime.store.save_logs(
                [
                    {
                        "app_id": "primary",
                        "session_id": "s-1",
                        "trace_id": "trace-1",
                        "event_class": "AgentTraceEvent",
                        "event_type": "agent.run.start",
                        "created_at": 1.0,
                        "payload": {"payload": {}},
                    }
                ]
            )
            events = client.get("/api/v1/telemetry/events?agent_id=primary")
            assert events.status_code == 200
            payload = events.json()["data"]
            assert payload["events"][0]["event_type"] == "agent.run.start"
            assert payload["path"].endswith("/primary/state.db")

def test_missing_telemetry_assets_fail_fast() -> None:
    with patch("mash.api.app.mount_telemetry_ui", side_effect=RuntimeError("missing telemetry assets")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": str(root)}):
                host = MashAgentHostBuilder().primary(_Spec(root, agent_id="primary"), agent_id="primary").build()
                try:
                    create_app(host, config=MashHostConfig())
                except RuntimeError as exc:
                    assert "missing telemetry assets" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected create_app() to fail when telemetry assets are missing")

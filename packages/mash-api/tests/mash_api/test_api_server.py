"""Integration tests for mash-api server composition."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.core.config import AgentConfig, SystemPrompt
from mash.core.context import Context, Response, ToolCall
from mash.core.llm import LLMProvider
from mash.memory.store import SQLiteStore
from mash.runtime import MashRuntimeDefinition
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry
from mash_api import MashAPIConfig, create_app


class _FakeLLMProvider(LLMProvider):
    def create_message(
        self,
        *,
        model: str,
        system: SystemPrompt,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
        betas: Optional[List[str]] = None,
        use_prompt_caching: bool = True,
    ) -> Any:
        raise NotImplementedError

    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _Definition(MashRuntimeDefinition):
    def __init__(self, root: Path, *, app_id: str = "primary") -> None:
        self.root = root
        self.app_id = app_id

    def get_app_id(self) -> str:
        return self.app_id

    def build_store(self) -> SQLiteStore:
        return SQLiteStore(self.root / f"{self.app_id}.db")

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.app_id, system_prompt="You are primary")

    def get_log_destination(self) -> Path:
        return self.root / "logs" / f"{self.app_id}.jsonl"


def _build_test_client(root: Path, *, api_key: str | None = None) -> TestClient:
    definition = _Definition(root)
    app = create_app(
        definition,
        config=MashAPIConfig(
            api_key=api_key,
            observability_log_path=root / "events.jsonl",
        ),
    )
    return TestClient(app)


def _response() -> Response:
    return Response(
        text="ok",
        context=Context(),
        metadata={"token_usage": {"input": 2, "output": 1}},
    )


def test_health_and_openapi_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "events.jsonl").write_text("", encoding="utf-8")
        with _build_test_client(root) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200
            payload = health.json()["data"]
            assert payload["status"] == "ok"
            assert payload["runtime"]["primary_agent_id"] == "primary"

            openapi = client.get("/openapi.json")
            assert openapi.status_code == 200
            paths = openapi.json().get("paths", {})
            expected = {
                "/api/v1/health",
                "/api/v1/interactions/invoke",
                "/api/v1/interactions/requests",
                "/api/v1/interactions/requests/{request_id}/events",
                "/api/v1/runtime/session",
                "/api/v1/runtime/subagents",
                "/api/v1/runtime/sessions/{session_id}/preferences",
                "/api/v1/runtime/sessions/{session_id}/app-data",
                "/api/v1/runtime/sessions/{session_id}/app-data/{key}",
                "/api/v1/runtime/sessions/{session_id}/history",
                "/api/v1/runtime/sessions/{session_id}/compact",
                "/api/v1/telemetry/events",
                "/api/v1/telemetry/events/stream",
                "/api/v1/telemetry/memory/search",
            }
            assert expected.issubset(set(paths.keys()))


def test_invoke_request_and_runtime_sse() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "events.jsonl").write_text("", encoding="utf-8")
        with _build_test_client(root) as client:
            runtime = client.app.state.runtime_state.host.get_agent("primary")
            with patch.object(runtime.agent, "run", return_value=_response()):
                invoke = client.post(
                    "/api/v1/interactions/invoke",
                    json={"message": "hello", "session_id": "s1"},
                )
                assert invoke.status_code == 200
                data = invoke.json()["data"]
                assert data["status"] == "completed"
                assert data["response"]["text"] == "ok"

                submitted = client.post(
                    "/api/v1/interactions/requests",
                    json={"message": "hello again", "session_id": "s1"},
                )
                assert submitted.status_code == 200
                request_id = submitted.json()["data"]["request_id"]

                with client.stream(
                    "GET",
                    f"/api/v1/interactions/requests/{request_id}/events",
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
                    assert "request.started" in names
                    assert names[-1] == "request.completed"


def test_runtime_control_routes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "events.jsonl").write_text("", encoding="utf-8")
        with _build_test_client(root) as client:
            runtime = client.app.state.runtime_state.host.get_agent("primary")
            with patch.object(runtime.agent, "run", return_value=_response()):
                invoke = client.post(
                    "/api/v1/interactions/invoke",
                    json={"message": "save history", "session_id": "s1"},
                )
                assert invoke.status_code == 200

            set_prefs = client.put(
                "/api/v1/runtime/sessions/s1/preferences",
                json={"preferences": {"tone": "brief"}},
            )
            assert set_prefs.status_code == 200

            get_prefs = client.get("/api/v1/runtime/sessions/s1/preferences")
            assert get_prefs.status_code == 200
            assert get_prefs.json()["data"]["preferences"] == {"tone": "brief"}

            set_data = client.put(
                "/api/v1/runtime/sessions/s1/app-data/focus",
                json={"value": {"path": "src"}},
            )
            assert set_data.status_code == 200

            listed = client.get("/api/v1/runtime/sessions/s1/app-data")
            assert listed.status_code == 200
            assert listed.json()["data"]["items"][0]["key"] == "focus"

            history = client.get("/api/v1/runtime/sessions/s1/history?limit=1")
            assert history.status_code == 200
            assert len(history.json()["data"]["turns"]) == 1

            with patch.object(runtime, "compact_session", return_value=("summary", "turn-1")):
                compact = client.post(
                    "/api/v1/runtime/sessions/s1/compact",
                    json={"reason": "manual", "session_total_tokens_reset": 0},
                )
            assert compact.status_code == 200
            assert compact.json()["data"]["summary_text"] == "summary"

            deleted = client.delete("/api/v1/runtime/sessions/s1/app-data/focus")
            assert deleted.status_code == 200
            assert deleted.json()["data"]["deleted"] is True


def test_observability_routes_and_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_path = root / "events.jsonl"
        log_path.write_text(
            "\n".join(
                [
                    json.dumps({"event_type": "ok.one"}),
                    "not-json",
                    json.dumps({"event_type": "ok.two"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with _build_test_client(root, api_key="secret") as client:
            unauthorized = client.get("/api/v1/health")
            assert unauthorized.status_code == 401
            assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"

            headers = {"X-API-Key": "secret"}
            events = client.get("/api/v1/telemetry/events?limit=10", headers=headers)
            assert events.status_code == 200
            payload = events.json()["data"]
            assert [item["event_type"] for item in payload["events"]] == ["ok.one", "ok.two"]

            search = client.get(
                "/api/v1/telemetry/memory/search?q=hello&app_id=demo",
                headers=headers,
            )
            assert search.status_code == 503
            assert search.json()["error"]["code"] == "MEMORY_SEARCH_UNAVAILABLE"

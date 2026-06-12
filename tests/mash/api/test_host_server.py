"""Integration tests for the public host API over hosted runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import MashHostConfig, create_app
from mash.api.telemetry_ui import TELEMETRY_API_KEY_COOKIE, get_telemetry_static_dir
from mash.runtime import Host, HostBuilder
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import TaskSpec, WorkflowSpec
from mash.workflows import dbos as workflow_dbos
from mash.workflows.service import workflow_task_session_id


@contextmanager
def _build_test_client(
    root: Path,
    *,
    api_key: str | None = None,
    workflow_enabled: bool = False,
    workflow_response_text: str = '{"last_run_ts":"2026-05-14T00:00:00Z"}',
):
    with patch.dict(
        os.environ,
        {
            "MASH_DATA_DIR": str(root),
            "MASH_DATABASE_URL": "",
        },
    ):
        builder = (
            HostBuilder()
            .agent(
                build_spec(agent_id="primary", response_text="primary-ok"),
                metadata=metadata(),
            )
            .agent(
                build_spec(
                    agent_id="research",
                    response_text="research-ok",
                ),
                metadata=metadata(),
            )
            .host(
                Host(
                    host_id="assistant",
                    primary="primary",
                    subagents=("research",),
                )
            )
        )
        if workflow_enabled:
            changelog_spec = build_spec(
                agent_id="changelog-agent",
                response_text=workflow_response_text,
            )
            builder = (
                builder.workflow(
                    WorkflowSpec(
                        workflow_id="changelog",
                        tasks=[
                            TaskSpec(
                                task_id="scan-codebase-and-append-changelog",
                                agent_spec=changelog_spec,
                            )
                        ],
                    )
                )
            )
        host = builder.build()
        app = create_app(
            host,
            config=MashHostConfig(
                runtime_database_url="postgresql://test/runtime",
                api_key=api_key,
            ),
        )
        with TestClient(app) as client:
            yield client


@dataclass
class _FakeWorkflowStatus:
    workflow_id: str
    status: str
    created_at: int = 1_700_000_000_000
    updated_at: int = 1_700_000_001_000
    output: dict[str, Any] | None = None
    error: Exception | None = None
    deduplication_id: str | None = None


def _save_workflow_turn(
    client: TestClient,
    *,
    run_id: str,
    user_message: str = "workflow input",
    agent_response: str = '{"status":"ok"}',
) -> str:
    runtime = client.app.state.runtime_state.pool.get_agent("changelog-agent")
    session_id = workflow_task_session_id(
        workflow_id="changelog",
        task_id="scan-codebase-and-append-changelog",
        run_id=run_id,
    )
    turn_id = f"trace-{run_id.replace(':', '-')}"
    asyncio.run(
        runtime.memory_store.save_turn(
            trace_id=turn_id,
            session_id=session_id,
            app_id="changelog-agent",
            user_message=user_message,
            agent_response=agent_response,
            signals={},
            session_total_tokens=0,
        )
    )
    return turn_id


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
            assert len(payload["deployment"]["agents"]) == 2
            assert payload["deployment"]["hosts"] == [
                {
                    "host_id": "assistant",
                    "primary": "primary",
                    "subagents": ["research"],
                    "workflows": [],
                }
            ]
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


def test_host_routes_define_inspect_and_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            listed = client.get("/api/v1/hosts")
            assert listed.status_code == 200
            assert [host["host_id"] for host in listed.json()["data"]["hosts"]] == [
                "assistant"
            ]

            described = client.get("/api/v1/hosts/assistant")
            assert described.status_code == 200
            payload = described.json()["data"]
            assert payload["primary"]["agent_id"] == "primary"
            assert payload["subagents"][0]["agent_id"] == "research"
            assert payload["subagents"][0]["metadata"]["display_name"] == "Research"

            missing = client.get("/api/v1/hosts/unknown")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "HOST_NOT_FOUND"

            # Idempotent define/replace over the API.
            defined = client.put(
                "/api/v1/hosts/solo",
                json={"primary": "research"},
            )
            assert defined.status_code == 200
            assert defined.json()["data"]["primary"]["agent_id"] == "research"
            assert [host["host_id"] for host in client.get("/api/v1/hosts").json()["data"]["hosts"]] == [
                "assistant",
                "solo",
            ]

            invalid = client.put(
                "/api/v1/hosts/bad",
                json={"primary": "missing-agent"},
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "INVALID_HOST"


def test_workflow_list_host_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            unfiltered = client.get("/api/v1/workflow")
            assert unfiltered.status_code == 200
            assert [
                workflow["workflow_id"]
                for workflow in unfiltered.json()["data"]["workflows"]
            ] == ["changelog"]

            # The default host has no attached workflows.
            empty = client.get("/api/v1/workflow", params={"host": "assistant"})
            assert empty.status_code == 200
            assert empty.json()["data"]["workflows"] == []

            attached = client.put(
                "/api/v1/hosts/assistant",
                json={
                    "primary": "primary",
                    "subagents": ["research"],
                    "workflows": ["changelog"],
                },
            )
            assert attached.status_code == 200

            filtered = client.get("/api/v1/workflow", params={"host": "assistant"})
            assert filtered.status_code == 200
            assert [
                workflow["workflow_id"]
                for workflow in filtered.json()["data"]["workflows"]
            ] == ["changelog"]

            missing = client.get("/api/v1/workflow", params={"host": "unknown"})
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "HOST_NOT_FOUND"


def test_host_request_routes_to_primary_and_streams_from_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/hosts/assistant/request",
                json={"message": "hello", "session_id": "s-host-1"},
            )
            assert submitted.status_code == 200
            accepted = submitted.json()["data"]
            assert accepted["agent_id"] == "primary"
            assert accepted["session_id"] == "s-host-1"
            request_id = accepted["request_id"]
            assert request_id

            payload = _collect_terminal_response(client, "primary", request_id)
            assert payload["response"]["text"] == "primary-ok"

            missing = client.post(
                "/api/v1/hosts/unknown/request",
                json={"message": "hello", "session_id": "s-host-2"},
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "HOST_NOT_FOUND"


def test_register_agent_skill_endpoint_registers_dynamic_skill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.post(
                "/api/v1/agent/primary/skill",
                json={
                    "type": "dynamic",
                    "name": "workflow:test:v1",
                    "description": "Test workflow skill.",
                    "content": "# Test workflow",
                },
            )

            assert response.status_code == 200
            assert response.json()["data"] == {
                "agent_id": "primary",
                "skill_name": "workflow:test:v1",
            }
            runtime = client.app.state.runtime_state.pool.get_agent("primary")
            assert runtime.skills.get("workflow:test:v1") is not None
            assert "Skill" in runtime.agent.tools


def test_register_agent_skill_endpoint_returns_not_found_for_unknown_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.post(
                "/api/v1/agent/missing/skill",
                json={
                    "type": "dynamic",
                    "name": "workflow:test:v1",
                    "content": "# Test workflow",
                },
            )

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


def test_register_agent_workflow_endpoint_registers_dynamic_workflow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.post(
                "/api/v1/agent/primary/workflow",
                json={
                    "workflow_id": "pilot-changelog",
                    "tasks": [
                        {
                            "task_id": "scan-recent-commits",
                            "agent_id": "primary",
                            "structured_output": {
                                "title": "WorkflowOutput",
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                    "metadata": {"source": "test"},
                    "task_message": {
                        "skill_name": "workflow:test:v1",
                    },
                },
            )

            assert response.status_code == 200
            assert response.json()["data"] == {
                "agent_id": "primary",
                "workflow_id": "pilot-changelog",
            }
            workflow = client.app.state.runtime_state.pool.get_workflow_registry().get(
                "pilot-changelog"
            )
            assert workflow.tasks[0].agent_id == "primary"
            assert workflow.tasks[0].structured_output == {
                "title": "WorkflowOutput",
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
            assert workflow.task_message.skill_name == "workflow:test:v1"


def test_register_agent_workflow_endpoint_returns_not_found_for_unknown_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.post(
                "/api/v1/agent/missing/workflow",
                json={
                    "workflow_id": "pilot-changelog",
                    "tasks": [{"task_id": "scan-recent-commits", "agent_id": "primary"}],
                    "task_message": {
                        "skill_name": "workflow:test:v1",
                    },
                },
            )

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


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


def test_agent_request_accepts_structured_output_schema_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": str(root),
                "MASH_DATABASE_URL": "",
            },
        ):
            host = HostBuilder().agent(
                build_spec(agent_id="primary", response_text='{"ok":true}'),
                metadata=metadata(),
            ).build()
            app = create_app(
                host,
                config=MashHostConfig(
                    runtime_database_url="postgresql://test/runtime",
                ),
            )
            with TestClient(app) as client:
                submitted = client.post(
                    "/api/v1/agent/primary/request",
                    json={
                        "message": "hello",
                        "session_id": "s1",
                        "structured_output": {
                            "title": "Result",
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                )
                assert submitted.status_code == 200
                request_id = submitted.json()["data"]["request_id"]
                payload = _collect_terminal_response(client, "primary", request_id)
                assert payload["response"]["structured_output"] == {"ok": True}


def test_agent_request_rejects_invalid_structured_output_schema_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={
                    "message": "hello",
                    "session_id": "s1",
                    "structured_output": ["not-a-schema"],
                },
            )

            assert submitted.status_code == 422


def test_session_signals_route_returns_definitions_and_turn_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            first = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s1"},
            )
            assert first.status_code == 200
            first_request_id = first.json()["data"]["request_id"]
            first_payload = _collect_terminal_response(client, "primary", first_request_id)

            second = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello again", "session_id": "s1"},
            )
            assert second.status_code == 200
            second_request_id = second.json()["data"]["request_id"]
            second_payload = _collect_terminal_response(client, "primary", second_request_id)

            response = client.get("/api/v1/agent/primary/sessions/s1/signals")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["agent_id"] == "primary"
            assert payload["session_id"] == "s1"
            assert set(payload["definitions"].keys()) == {"unused_tools", "unused_tool_tokens"}
            assert payload["definitions"]["unused_tools"]["value_type"] == "string_list"
            assert payload["definitions"]["unused_tool_tokens"]["computed_at"] == "turn_complete"
            assert [turn["turn_id"] for turn in payload["turns"]] == [
                first_payload["turn_id"],
                second_payload["turn_id"],
            ]
            assert "unused_tools" in payload["turns"][0]["signals"]
            assert "unused_tool_tokens" in payload["turns"][0]["signals"]

            limited = client.get("/api/v1/agent/primary/sessions/s1/signals", params={"limit": 1})
            assert limited.status_code == 200
            limited_payload = limited.json()["data"]
            assert [turn["turn_id"] for turn in limited_payload["turns"]] == [
                second_payload["turn_id"]
            ]


def test_session_signals_route_returns_definitions_for_empty_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get("/api/v1/agent/primary/sessions/missing/signals")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["agent_id"] == "primary"
            assert payload["session_id"] == "missing"
            assert set(payload["definitions"].keys()) == {"unused_tools", "unused_tool_tokens"}
            assert payload["turns"] == []


def test_session_signals_route_rejects_blank_session_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get("/api/v1/agent/primary/sessions/%20/signals")
            assert response.status_code == 400
            payload = response.json()["error"]
            assert payload["code"] == "INVALID_REQUEST"
            assert payload["message"] == "session_id is required"


def test_session_signals_route_returns_not_found_for_unknown_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get("/api/v1/agent/missing/sessions/s1/signals")
            assert response.status_code == 404
            payload = response.json()["error"]
            assert payload["code"] == "AGENT_NOT_FOUND"


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
                "MASH_DATABASE_URL": "",
            },
        ):
            host = (
                HostBuilder()
                .agent(
                    build_spec(
                        agent_id="primary",
                        response_text="primary-ok",
                        delay_seconds=0.25,
                    ),
                    metadata=metadata(),
                )
                .build()
            )
            app = create_app(
                host,
                config=MashHostConfig(runtime_database_url="postgresql://test/runtime"),
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


def test_api_event_logging_captures_api_request_metadata_and_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret") as client:
            headers = {"X-API-Key": "secret"}
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s-1"},
                headers=headers,
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            _collect_terminal_response(client, "primary", request_id, headers=headers)

            events = client.get(
                "/api/v1/telemetry/api/events",
                params={"path": "/api/v1/agent/primary/request"},
                headers=headers,
            )
            assert events.status_code == 200
            payload = events.json()["data"]
            assert payload["source"] == "api_event_log"
            logged = payload["events"][-1]
            assert logged["event_type"] == "api.request.complete"
            assert logged["method"] == "POST"
            assert logged["path"] == "/api/v1/agent/primary/request"
            assert logged["status_code"] == 200
            assert logged["request_headers"]["x-api-key"] == "[REDACTED]"
            assert logged["request_body"]["capture_status"] == "captured"
            assert logged["request_body"]["json"] == {"message": "hello", "session_id": "s-1"}
            assert "request_id" not in logged
            assert "trace_id" not in logged
            assert "x-mash-request-id" not in logged["request_headers"]
            assert "x-mash-trace-id" not in logged["request_headers"]


def test_api_event_search_filters_by_status_and_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            missing = client.get("/api/v1/agent/missing")
            assert missing.status_code == 404
            ok = client.get("/api/v1/agent")
            assert ok.status_code == 200

            search = client.post(
                "/api/v1/telemetry/api/events/search",
                json={"path_prefix": "/api/v1/agent", "status_code_min": 400, "status_code_max": 499},
            )
            assert search.status_code == 200
            events = search.json()["data"]["events"]
            assert any(event["path"] == "/api/v1/agent/missing" for event in events)
            assert all(400 <= event["status_code"] <= 499 for event in events)


def test_api_logging_skips_configured_paths_and_truncates_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": str(root),
                "MASH_DATABASE_URL": "",
            },
        ):
            host = HostBuilder().agent(build_spec(agent_id="primary", response_text="ok"), metadata=metadata()).build()
            app = create_app(
                host,
                config=MashHostConfig(
                    runtime_database_url="postgresql://test/runtime",
                    api_log_body_max_bytes=12,
                    api_log_excluded_paths=("/api/v1/health",),
                ),
            )
            with TestClient(app) as client:
                health = client.get("/api/v1/health")
                assert health.status_code == 200
                submitted = client.post(
                    "/api/v1/agent/primary/request",
                    json={"message": "this body should be truncated", "session_id": "s-1"},
                )
                assert submitted.status_code == 200

                health_logs = client.get(
                    "/api/v1/telemetry/api/events",
                    params={"path": "/api/v1/health"},
                )
                assert health_logs.status_code == 200
                assert health_logs.json()["data"]["events"] == []

                request_logs = client.get(
                    "/api/v1/telemetry/api/events",
                    params={"path": "/api/v1/agent/primary/request"},
                )
                logged = request_logs.json()["data"]["events"][-1]
                assert logged["request_body"]["truncated"] is True
                assert logged["request_body"]["captured_bytes"] == 12


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
                f"/api/v1/agent/primary/session/s-1/trace/{trace_id}/reasoning",
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
                "/api/v1/agent/primary/session/%20/trace/trace-1/reasoning",
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


def test_workflow_routes_are_available_without_registered_workflows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            listed = client.get("/api/v1/workflow")
            assert listed.status_code == 200
            assert listed.json()["data"]["workflows"] == []

            submitted = client.post("/api/v1/workflow/changelog/run", json={})
            assert submitted.status_code == 404
            assert submitted.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_workflow_routes_list_and_run_registered_workflows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            listed = client.get("/api/v1/workflow")
            assert listed.status_code == 200
            assert listed.json()["data"]["workflows"] == [
                {
                    "workflow_id": "changelog",
                    "tasks": [
                        {
                            "task_id": "scan-codebase-and-append-changelog",
                            "agent_id": "changelog-agent",
                        }
                    ],
                }
            ]

            async def start_workflow_run(**kwargs):
                assert kwargs["workflow_input"] == {}
                return "mw:host-1:changelog:abc"

            async def get_workflow_status(_run_id):
                return _FakeWorkflowStatus(
                    workflow_id="mw:host-1:changelog:abc",
                    status="ENQUEUED",
                )

            with patch.object(
                workflow_dbos,
                "start_workflow_run",
                start_workflow_run,
            ), patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
                submitted = client.post("/api/v1/workflow/changelog/run", json={})
            assert submitted.status_code == 200
            payload = submitted.json()["data"]
            assert payload["workflow_id"] == "changelog"
            assert payload["status"] == "queued"
            assert payload["run_id"] == "mw:host-1:changelog:abc"


def test_workflow_run_accepts_input_object() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            async def start_workflow_run(**kwargs):
                assert kwargs["workflow_input"] == {"target_agent_id": "primary"}
                return "mw:host-1:changelog:abc"

            async def get_workflow_status(_run_id):
                return _FakeWorkflowStatus(
                    workflow_id="mw:host-1:changelog:abc",
                    status="ENQUEUED",
                )

            with patch.object(
                workflow_dbos,
                "start_workflow_run",
                start_workflow_run,
            ), patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
                submitted = client.post(
                    "/api/v1/workflow/changelog/run",
                    json={"input": {"target_agent_id": "primary"}},
                )
            assert submitted.status_code == 200


def test_workflow_run_rejects_non_object_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            response = client.post(
                "/api/v1/workflow/changelog/run",
                json={"input": ["bad"]},
            )
            assert response.status_code == 422


def test_workflow_run_returns_not_found_for_unknown_workflow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            response = client.post("/api/v1/workflow/missing/run", json={})
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_workflow_run_returns_conflict_for_duplicate_active_dedup_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            async def start_workflow_run(**_kwargs):
                raise workflow_dbos.WorkflowDeduplicatedError(
                    "mw:host-1:changelog:old"
                )

            with patch.object(workflow_dbos, "start_workflow_run", start_workflow_run):
                response = client.post(
                    "/api/v1/workflow/changelog/run",
                    json={"dedup_key": "manual"},
                )
            assert response.status_code == 409
            payload = response.json()["error"]
            assert payload["code"] == "WORKFLOW_DUPLICATE_RUN"
            assert payload["details"]["run_id"] == "mw:host-1:changelog:old"


def test_workflow_run_status_endpoint_returns_dbos_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            host_id = client.app.state.runtime_state.pool.runner_id
            run_id = f"mw:{host_id}:changelog:abc"

            async def get_workflow_status(_run_id):
                return _FakeWorkflowStatus(
                    workflow_id=run_id,
                    status="SUCCESS",
                    output={"task_states": {"digest-traces": {"status": "ok"}}},
                    deduplication_id=None,
                )

            with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
                response = client.get(f"/api/v1/workflow/changelog/runs/{run_id}")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["run_id"] == run_id
            assert payload["workflow_id"] == "changelog"
            assert payload["status"] == "completed"
            assert payload["output"] == {"task_states": {"digest-traces": {"status": "ok"}}}


def test_workflow_runs_endpoint_lists_memory_turn_summaries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            run_id = "mw:h_TI1UUyBX5w8Q:changelog:bHfMwMfMsPDPHI60"
            turn_id = _save_workflow_turn(
                client,
                run_id=run_id,
                user_message="summarize run",
                agent_response='{"summary":"done"}',
            )

            assert not hasattr(workflow_dbos, "list_workflow_statuses")
            response = client.get("/api/v1/workflow/changelog/runs")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["workflow_id"] == "changelog"
            assert len(payload["runs"]) == 1
            run = payload["runs"][0]
            assert run["run_id"] == run_id
            assert run["workflow_id"] == "changelog"
            assert run["dedup_key"] is None
            assert run["status"] == "completed"
            assert run["error"] is None
            assert "output" not in payload["runs"][0]
            assert run["summary"] == {
                "turn_id": turn_id,
                "session_id": workflow_task_session_id(
                    workflow_id="changelog",
                    task_id="scan-codebase-and-append-changelog",
                    run_id=run_id,
                ),
                "task_id": "scan-codebase-and-append-changelog",
                "agent_id": "changelog-agent",
                "user_message": "summarize run",
                "agent_response": '{"summary":"done"}',
            }


def test_workflow_runs_endpoint_returns_empty_for_non_completed_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            _save_workflow_turn(client, run_id="mw:h_static:changelog:abc")

            assert not hasattr(workflow_dbos, "list_workflow_statuses")
            response = client.get(
                "/api/v1/workflow/changelog/runs",
                params={
                    "status": "failed",
                },
            )
            assert response.status_code == 200
            assert response.json()["data"]["runs"] == []


def test_workflow_runs_endpoint_respects_api_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret", workflow_enabled=True) as client:
            unauthorized = client.get("/api/v1/workflow/changelog/runs")
            assert unauthorized.status_code == 401

            assert not hasattr(workflow_dbos, "list_workflow_statuses")
            authorized = client.get(
                "/api/v1/workflow/changelog/runs",
                headers={"x-api-key": "secret"},
            )
            assert authorized.status_code == 200
            assert authorized.json()["data"]["runs"] == []


def test_workflow_run_events_streams_workflow_and_agent_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            pool = client.app.state.runtime_state.pool
            host_id = pool.runner_id
            run_id = f"mw:{host_id}:changelog:abc"
            task_id = "scan-codebase-and-append-changelog"
            session_id = workflow_task_session_id(
                workflow_id="changelog",
                task_id=task_id,
                run_id=run_id,
            )
            runtime = pool.get_agent("changelog-agent")

            async def list_events(
                app_id: str,
                *,
                session_id: str | None = None,
                trace_id: str | None = None,
                after_event_id: int = 0,
                limit: int | None = None,
            ):
                del trace_id, limit
                events = [
                    RuntimeEvent(
                        event_id=1,
                        request_id="req-1",
                        app_id="changelog-agent",
                        agent_id="changelog-agent",
                        session_id=session_id,
                        event_type=RuntimeEventType.REQUEST_ACCEPTED.value,
                    ),
                    RuntimeEvent(
                        event_id=2,
                        request_id="req-1",
                        app_id="changelog-agent",
                        agent_id="changelog-agent",
                        session_id=session_id,
                        trace_id="trace-1",
                        event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                        payload={"action_type": "response", "assistant_text": "{}"},
                    ),
                    RuntimeEvent(
                        event_id=3,
                        request_id="req-1",
                        app_id="changelog-agent",
                        agent_id="changelog-agent",
                        session_id=session_id,
                        trace_id="trace-1",
                        event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                        payload={"request_id": "req-1", "response": {"text": "{}"}},
                    ),
                ]
                return [
                    event
                    for event in events
                    if event.app_id == app_id and event.event_id > after_event_id
                ]

            runtime.runtime_store.list_events = list_events

            with patch.object(
                workflow_dbos,
                "get_workflow_status",
                side_effect=AssertionError("DBOS status must not be used"),
            ):
                with client.stream(
                    "GET",
                    f"/api/v1/workflow/changelog/runs/{run_id}/events",
                ) as stream:
                    assert stream.status_code == 200
                    assert "text/event-stream" in stream.headers["content-type"]
                    events = _collect_sse_events(stream)

            names = [event["event"] for event in events]
            assert names == [
                "workflow.task.started",
                "request.accepted",
                "agent.trace",
                "request.completed",
                "workflow.task.completed",
            ]
            completed = events[-2]["data"]
            assert completed["workflow_id"] == "changelog"
            assert completed["run_id"] == run_id
            assert completed["task_id"] == task_id
            assert completed["task_agent_id"] == "changelog-agent"


def test_workflow_run_events_returns_not_found_for_unknown_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            response = client.get(
                "/api/v1/workflow/changelog/runs/mw:wrong:changelog:abc/events"
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_workflow_run_events_respects_api_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret", workflow_enabled=True) as client:
            pool = client.app.state.runtime_state.pool
            run_id = f"mw:{pool.runner_id}:changelog:abc"
            unauthorized = client.get(f"/api/v1/workflow/changelog/runs/{run_id}/events")
            assert unauthorized.status_code == 401

            runtime = pool.get_agent("changelog-agent")

            async def list_events(
                app_id: str,
                *,
                session_id: str | None = None,
                trace_id: str | None = None,
                after_event_id: int = 0,
                limit: int | None = None,
            ):
                del trace_id, limit
                events = [
                    RuntimeEvent(
                        event_id=1,
                        request_id="req-1",
                        app_id="changelog-agent",
                        agent_id="changelog-agent",
                        session_id=session_id,
                        event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                        payload={"request_id": "req-1", "response": {"text": "{}"}},
                    )
                ]
                return [
                    event
                    for event in events
                    if event.app_id == app_id and event.event_id > after_event_id
                ]

            runtime.runtime_store.list_events = list_events

            with patch.object(
                workflow_dbos,
                "get_workflow_status",
                side_effect=AssertionError("DBOS status must not be used"),
            ):
                with client.stream(
                    "GET",
                    f"/api/v1/workflow/changelog/runs/{run_id}/events",
                    headers={"Authorization": "Bearer secret"},
                ) as stream:
                    assert stream.status_code == 200
                    assert "text/event-stream" in stream.headers["content-type"]


def test_missing_telemetry_assets_fail_fast() -> None:
    with patch("mash.api.app.mount_telemetry_ui", side_effect=RuntimeError("missing telemetry assets")):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "MASH_DATA_DIR": str(root),
                    "MASH_DATABASE_URL": "",
                },
            ):
                host = HostBuilder().agent(
                    build_spec(agent_id="primary", response_text="primary-ok"),
                    metadata=metadata(),
                ).build()
                try:
                    create_app(
                        host,
                        config=MashHostConfig(runtime_database_url="postgresql://test/runtime"),
                    )
                except RuntimeError as exc:
                    assert "missing telemetry assets" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected create_app() to fail when telemetry assets are missing")


def _collect_terminal_response(
    client: TestClient,
    agent_id: str,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    with client.stream(
        "GET",
        f"/api/v1/agent/{agent_id}/request/{request_id}/events",
        headers=headers,
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


def _collect_sse_events(stream) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event = None
    for line in stream.iter_lines():
        if not line:
            continue
        text = line if isinstance(line, str) else line.decode("utf-8")
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            current_event = text.split(":", 1)[1].strip()
            continue
        if text.startswith("data:") and current_event:
            payload = json.loads(text.split(":", 1)[1].strip())
            events.append({"event": current_event, "data": payload})
            if current_event in {"workflow.task.completed", "workflow.task.error", "workflow.error"}:
                break
    return events

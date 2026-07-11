"""Integration tests for the public host API over hosted runtimes."""

# Tests seed the in-memory store fakes through their private state.
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest

from mash.api import MashHostConfig, create_app
from mash.api.admin_ui import get_admin_static_dir
from mash.api.routes.common import API_KEY_COOKIE
from mash.runtime import Host, HostBuilder
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import AgentStep, WorkflowSpec
from mash.workflows.store import WorkflowRunRecord, WorkflowStepEventRecord
from mash.workflows import dbos as workflow_dbos


class ChangelogInput(BaseModel):
    target_agent_id: str | None = None
    count: int = 1


MASHER_WORKFLOW_IDS = [
    "masher-trace-digest",
    "masher-online-eval-curation",
    "gen-synthetic-evals",
    "run-experiment",
]


@pytest.fixture(autouse=True)
def _stub_eval_agent_llms():
    """Keep API tests independent from developer and CI provider credentials."""
    with patch(
        "mash.agents.masher.spec.EvalAgentSpec.build_llm",
        side_effect=lambda: build_spec(
            agent_id="eval-agent", response_text="{}"
        ).build_llm(),
    ), patch(
        "mash.agents.masher.spec.EvalJudgeAgentSpec.build_llm",
        side_effect=lambda: build_spec(
            agent_id="eval-judge-agent", response_text="{}"
        ).build_llm(),
    ):
        yield


def _runtime_state(client: TestClient) -> Any:
    """The app is a FastAPI instance; TestClient types it as a bare ASGI callable."""
    return cast(FastAPI, client.app).state.runtime_state


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
                        input_model=ChangelogInput,
                        steps=[
                            AgentStep(
                                step_id="scan-codebase-and-append-changelog",
                                agent_spec=changelog_spec,
                                output={"type": "object"},
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
    runtime = _runtime_state(client).pool.get_agent("changelog-agent")
    turn_id = f"trace-{run_id.replace(':', '-')}"
    asyncio.run(
        runtime.memory_store.save_turn(
            trace_id=turn_id,
            session_id=f"session-{run_id.replace(':', '-')}",
            app_id="changelog-agent",
            user_message=user_message,
            agent_response=agent_response,
            signals={},
            session_total_tokens=0,
            workflow_id="changelog",
            workflow_run_id=run_id,
            task_id="scan-codebase-and-append-changelog",
            replayable=False,
        )
    )
    return turn_id


def test_health_and_agent_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            admin = client.get("/admin")
            assert admin.status_code == 200
            assert "text/html" in admin.headers["content-type"]

            health = client.get("/api/v1/health")
            assert health.status_code == 200
            payload = health.json()["data"]
            assert payload["service"] == "mash-api"
            assert len(payload["deployment"]["agents"]) == 4
            assert payload["deployment"]["hosts"] == [
                {
                    "host_id": "assistant",
                    "primary": "primary",
                    "subagents": ["research"],
                    "workflows": MASHER_WORKFLOW_IDS,
                }
            ]
            assert payload["observability"]["memory"]["search_available"] is True

            agents = client.get("/api/v1/agent")
            assert agents.status_code == 200
            assert len(agents.json()["data"]["agents"]) == 4
            assert {
                item["agent_id"] for item in agents.json()["data"]["agents"]
            } == {"primary", "research", "eval-agent", "eval-judge-agent"}

            static_dir = get_admin_static_dir()
            asset_paths = sorted(
                path.relative_to(static_dir).as_posix()
                for path in (static_dir / "assets").iterdir()
            )
            assert asset_paths

            asset = client.get(f"/admin/{asset_paths[0]}")
            assert asset.status_code == 200

            spa = client.get("/admin/logs")
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


def test_host_snapshot_route_returns_live_composition_and_specs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get("/api/v1/hosts/assistant/snapshot")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["host_composition"] == {
                "host_id": "assistant",
                "primary": "primary",
                "subagents": ["research"],
            }
            assert set(payload["agent_spec_snapshot"]) == {"primary", "research"}

            missing = client.get("/api/v1/hosts/unknown/snapshot")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "HOST_NOT_FOUND"


def test_workflow_list_host_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            unfiltered = client.get("/api/v1/workflow")
            assert unfiltered.status_code == 200
            assert [
                workflow["workflow_id"]
                for workflow in unfiltered.json()["data"]["workflows"]
            ] == [*MASHER_WORKFLOW_IDS, "changelog"]

            defaults = client.get("/api/v1/workflow", params={"host": "assistant"})
            assert defaults.status_code == 200
            assert [
                workflow["workflow_id"]
                for workflow in defaults.json()["data"]["workflows"]
            ] == MASHER_WORKFLOW_IDS

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
            ] == [*MASHER_WORKFLOW_IDS, "changelog"]

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
            runtime = _runtime_state(client).pool.get_agent("primary")
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
            assert [turn["trace_id"] for turn in payload["turns"]] == [
                first_payload["trace_id"],
                second_payload["trace_id"],
            ]
            assert "unused_tools" in payload["turns"][0]["signals"]
            assert "unused_tool_tokens" in payload["turns"][0]["signals"]

            limited = client.get("/api/v1/agent/primary/sessions/s1/signals", params={"limit": 1})
            assert limited.status_code == 200
            limited_payload = limited.json()["data"]
            assert [turn["trace_id"] for turn in limited_payload["turns"]] == [
                second_payload["trace_id"]
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


def test_admin_ui_bootstraps_auth_cookie() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, api_key="secret") as client:
            unauthorized = client.get("/api/v1/health")
            assert unauthorized.status_code == 401

            admin = client.get("/admin")
            assert admin.status_code == 200
            assert client.cookies.get(API_KEY_COOKIE) == "secret"

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


def test_telemetry_sessions_rollup_from_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s-roll"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            _collect_terminal_response(client, "primary", request_id)

            listed = client.get("/api/v1/telemetry/sessions")
            assert listed.status_code == 200
            sessions = listed.json()["data"]["sessions"]
            rolled = next(s for s in sessions if s["session_id"] == "s-roll")
            assert rolled["owner_agent_id"] == "primary"
            assert rolled["trace_count"] >= 1
            assert "started_at" in rolled and "total_tokens" in rolled

            # The session total equals the sum of its traces' token totals.
            traces = client.get(
                "/api/v1/telemetry/traces?session_id=s-roll&limit=100"
            ).json()["data"]["traces"]
            assert traces and all("total_tokens" in t for t in traces)
            assert sum(t["total_tokens"] for t in traces) == rolled["total_tokens"]

            # /session (get_session) reports the same event-log total.
            info = client.get("/api/v1/agent/primary/sessions/s-roll").json()["data"]
            assert info["total_tokens"] == rolled["total_tokens"]

            # Agent filter matches sessions the agent participated in.
            scoped = client.get("/api/v1/telemetry/sessions?agent_id=primary")
            assert scoped.status_code == 200
            assert all(
                "primary" in s["agent_ids"]
                for s in scoped.json()["data"]["sessions"]
            )
            none = client.get("/api/v1/telemetry/sessions?agent_id=nonexistent")
            assert none.json()["data"]["sessions"] == []
            assert none.json()["data"]["total"] == 0


def test_telemetry_sessions_participant_and_workflow_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            # Primary owns the session; research participates as a subagent.
            submitted = client.post(
                "/api/v1/agent/primary/request",
                json={"message": "hello", "session_id": "s-multi"},
            )
            assert submitted.status_code == 200
            request_id = submitted.json()["data"]["request_id"]
            _collect_terminal_response(client, "primary", request_id)

            store = _runtime_state(client).pool.get_agent(
                "primary"
            ).runtime_store
            asyncio.run(
                store.append_event(
                    RuntimeEvent(
                        event_id=0,
                        request_id=None,
                        request_seq=None,
                        trace_id="t-sub",
                        app_id="research",
                        agent_id="research",
                        session_id="s-multi",
                        host_id="assistant",
                        workflow_id="changelog",
                        workflow_run_id="run-1",
                        event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                        loop_index=None,
                        step_key=None,
                        dedupe_key=None,
                        payload={"token_usage": {"input": 5, "output": 7}},
                        created_at=9_999_999_999.0,
                    )
                )
            )

            # The session is owned by primary but research participated, so
            # filtering by research must still return it.
            scoped = client.get("/api/v1/telemetry/sessions?agent_id=research")
            data = scoped.json()["data"]
            assert [s["session_id"] for s in data["sessions"]] == ["s-multi"]
            assert data["sessions"][0]["owner_agent_id"] == "primary"
            assert "research" in data["sessions"][0]["agent_ids"]
            assert data["total"] == 1

            # Workflow filter matches sessions where the workflow ran.
            by_workflow = client.get(
                "/api/v1/telemetry/sessions?workflow_id=changelog"
            )
            wf_data = by_workflow.json()["data"]
            assert [s["session_id"] for s in wf_data["sessions"]] == ["s-multi"]
            assert wf_data["total"] == 1
            no_wf = client.get("/api/v1/telemetry/sessions?workflow_id=absent")
            assert no_wf.json()["data"]["sessions"] == []

            # Unfiltered listing reports the full count alongside the page.
            listed = client.get("/api/v1/telemetry/sessions")
            assert listed.json()["data"]["total"] == len(
                listed.json()["data"]["sessions"]
            )


def test_command_events_ingest_and_list_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            start = client.post(
                "/api/v1/telemetry/command-events",
                json={
                    "agent_id": "primary",
                    "event_type": "command.start",
                    "session_id": "s-1",
                    "command_name": "/help",
                    "args": "topics",
                },
            )
            assert start.status_code == 200
            done = client.post(
                "/api/v1/telemetry/command-events",
                json={
                    "agent_id": "primary",
                    "event_type": "command.complete",
                    "session_id": "s-1",
                    "command_name": "/help",
                    "duration_ms": 12,
                },
            )
            assert done.status_code == 200

            listed = client.get(
                "/api/v1/telemetry/command-events?agent_id=primary&session_id=s-1"
            )
            assert listed.status_code == 200
            events = listed.json()["data"]["events"]
            assert [e["event_type"] for e in events] == [
                "command.start",
                "command.complete",
            ]
            assert events[0]["payload"]["command_name"] == "/help"
            assert events[0]["payload"]["args"] == "topics"
            assert events[1]["payload"]["duration_ms"] == 12

            # Command events must not leak into the generic event feed's
            # reasoning, but they share the store — the prefix filter scopes them.
            bad = client.post(
                "/api/v1/telemetry/command-events",
                json={"agent_id": "primary", "event_type": "agent.run.start"},
            )
            assert bad.status_code == 400


def test_feedback_submit_and_list_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            submitted = client.post(
                "/api/v1/feedback",
                json={
                    "agent_id": "primary",
                    "message": "the trace output is hard to read",
                    "session_id": "s-1",
                    "request_id": "r-9",
                },
            )
            assert submitted.status_code == 200
            stored = submitted.json()["data"]["feedback"]
            assert stored["feedback_id"] > 0
            assert stored["feedback_type"] == "text"

            listed = client.get("/api/v1/feedback?agent_id=primary&after=0")
            assert listed.status_code == 200
            payload = listed.json()["data"]
            assert payload["after"] == 0
            messages = [item["message"] for item in payload["feedback"]]
            assert "the trace output is hard to read" in messages

            matched = client.get("/api/v1/feedback?agent_id=primary&after=0&q=trace")
            assert matched.status_code == 200
            assert len(matched.json()["data"]["feedback"]) == 1

            missing = client.get("/api/v1/feedback?agent_id=primary&q=trace")
            assert missing.status_code == 422


def test_feedback_list_returns_not_found_for_unknown_agent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            response = client.get("/api/v1/feedback?agent_id=ghost&after=0")
            assert response.status_code == 404


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


def test_workflow_routes_include_default_workflows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root) as client:
            listed = client.get("/api/v1/workflow")
            assert listed.status_code == 200
            assert [
                workflow["workflow_id"]
                for workflow in listed.json()["data"]["workflows"]
            ] == MASHER_WORKFLOW_IDS

            submitted = client.post("/api/v1/workflow/changelog/run", json={})
            assert submitted.status_code == 404
            assert submitted.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"

            legacy_activity = client.get("/api/v1/telemetry/workflows")
            assert legacy_activity.status_code == 404


def test_workflow_routes_list_and_run_registered_workflows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            listed = client.get("/api/v1/workflow")
            assert listed.status_code == 200
            workflows = listed.json()["data"]["workflows"]
            changelog = next(
                item for item in workflows if item["workflow_id"] == "changelog"
            )
            assert changelog == {
                    "workflow_id": "changelog",
                    "display_name": "changelog",
                    "description": "",
                    "mode": "pipeline",
                    "step_count": 1,
                    "step_kinds": {"code": 0, "agent": 1},
                    "step_preview": [
                        {
                            "ordinal": 0,
                            "step_id": "scan-codebase-and-append-changelog",
                            "kind": "agent",
                            "agent_id": "changelog-agent",
                        }
                    ],
                    "history_available": True,
                    "latest_run": None,
                }

            described = client.get("/api/v1/workflow/changelog")
            assert described.status_code == 200
            definition = described.json()["data"]
            assert definition["mode"] == "pipeline"
            assert "target_agent_id" in definition["input_schema"]["properties"]
            assert definition["steps"] == [
                {
                    "ordinal": 0,
                    "step_id": "scan-codebase-and-append-changelog",
                    "kind": "agent",
                    "input_schema": None,
                    "output_schema": {"type": "object"},
                    "timeout_s": None,
                    "agent_id": "changelog-agent",
                    "skill_name": None,
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


def test_workflow_run_rejects_invalid_typed_input_before_enqueue() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            with patch.object(workflow_dbos, "start_workflow_run") as start:
                response = client.post(
                    "/api/v1/workflow/changelog/run",
                    json={"input": {"count": "not-an-integer"}},
                )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "WORKFLOW_INPUT_INVALID"
            assert response.json()["error"]["details"]["errors"][0]["loc"] == [
                "count"
            ]
            start.assert_not_called()


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
            host_id = _runtime_state(client).pool.runner_id
            run_id = f"mw:{host_id}:changelog:abc"

            async def get_workflow_status(_run_id):
                return _FakeWorkflowStatus(
                    workflow_id=run_id,
                    status="SUCCESS",
                    output={"summary": "done"},
                    deduplication_id=None,
                )

            with patch.object(workflow_dbos, "get_workflow_status", get_workflow_status):
                response = client.get(f"/api/v1/workflow/changelog/runs/{run_id}")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["run_id"] == run_id
            assert payload["workflow_id"] == "changelog"
            assert payload["status"] == "completed"
            assert payload["result"] == {"summary": "done"}
            assert payload["workflow_input"] is None


def test_workflow_runs_endpoint_lists_store_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            pool = _runtime_state(client).pool
            run_id = f"mw:{pool.runner_id}:changelog:abc"
            pool.get_workflow_store()._runs[run_id] = WorkflowRunRecord(
                run_id=run_id,
                workflow_id="changelog",
                status="completed",
                workflow_input={"count": 2},
                result={"summary": "done"},
                session_id="session-1",
                created_at=1.0,
                started_at=1.0,
                finished_at=2.0,
            )

            response = client.get("/api/v1/workflow/changelog/runs")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert payload["workflow_id"] == "changelog"
            assert len(payload["runs"]) == 1
            run = payload["runs"][0]
            assert run["run_id"] == run_id
            assert run["status"] == "completed"
            assert "result" not in run
            assert payload["limit"] == 50
            assert payload["offset"] == 0
            assert payload["has_more"] is False

            detail = client.get(f"/api/v1/workflow/changelog/runs/{run_id}")
            assert detail.status_code == 200
            run_detail = detail.json()["data"]
            assert run_detail["workflow_input"] == {"count": 2}
            assert run_detail["session_id"] == "session-1"
            assert run_detail["result"] == {"summary": "done"}

            catalog = client.get("/api/v1/workflow").json()["data"]["workflows"]
            changelog = next(
                item for item in catalog if item["workflow_id"] == "changelog"
            )
            assert changelog["latest_run"] == {
                "run_id": run_id,
                "status": "completed",
                "created_at": 1.0,
                "started_at": 1.0,
                "finished_at": 2.0,
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


def test_workflow_runs_endpoint_reports_more_pages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            pool = _runtime_state(client).pool
            for index in range(2):
                run_id = f"mw:{pool.runner_id}:changelog:{index}"
                pool.get_workflow_store()._runs[run_id] = WorkflowRunRecord(
                    run_id=run_id,
                    workflow_id="changelog",
                    status="completed",
                    created_at=float(index + 1),
                )

            first_page = client.get(
                "/api/v1/workflow/changelog/runs",
                params={"limit": 1},
            ).json()["data"]
            second_page = client.get(
                "/api/v1/workflow/changelog/runs",
                params={"limit": 1, "offset": 1},
            ).json()["data"]

            assert first_page["runs"][0]["run_id"].endswith(":1")
            assert first_page["has_more"] is True
            assert second_page["runs"][0]["run_id"].endswith(":0")
            assert second_page["has_more"] is False


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


def test_workflow_run_events_streams_step_events_from_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _build_test_client(root, workflow_enabled=True) as client:
            pool = _runtime_state(client).pool
            run_id = f"mw:{pool.runner_id}:changelog:abc"
            store = pool.get_workflow_store()
            store._runs[run_id] = WorkflowRunRecord(
                run_id=run_id,
                workflow_id="changelog",
                status="completed",
                result={"ok": True},
                created_at=1.0,
                finished_at=2.0,
            )
            store._events.append(
                WorkflowStepEventRecord(
                    run_id=run_id,
                    workflow_id="changelog",
                    step_id="scan-codebase-and-append-changelog",
                    attempt=1,
                    event_type="step.started",
                    seq=1,
                    at=1.0,
                    payload={},
                )
            )

            with client.stream(
                "GET", f"/api/v1/workflow/changelog/runs/{run_id}/events"
            ) as stream:
                assert stream.status_code == 200
                assert "text/event-stream" in stream.headers["content-type"]
                events = _collect_sse_events(stream)

            names = [event["event"] for event in events]
            assert "step.started" in names
            assert names[-1] == "workflow.completed"
            assert events[-1]["data"]["run_id"] == run_id


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
            pool = _runtime_state(client).pool
            run_id = f"mw:{pool.runner_id}:changelog:abc"
            unauthorized = client.get(f"/api/v1/workflow/changelog/runs/{run_id}/events")
            assert unauthorized.status_code == 401

            pool.get_workflow_store()._runs[run_id] = WorkflowRunRecord(
                run_id=run_id,
                workflow_id="changelog",
                status="completed",
                result={"ok": True},
                created_at=1.0,
                finished_at=2.0,
            )

            with client.stream(
                "GET",
                f"/api/v1/workflow/changelog/runs/{run_id}/events",
                headers={"Authorization": "Bearer secret"},
            ) as stream:
                assert stream.status_code == 200
                assert "text/event-stream" in stream.headers["content-type"]


def test_missing_admin_assets_degrade_gracefully() -> None:
    # The admin SPA is best-effort: a deployment that never built the bundle
    # still serves the API, it just does not expose the /admin route.
    with patch("mash.api.admin_ui.admin_assets_available", return_value=False):
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
                app = create_app(
                    host,
                    config=MashHostConfig(runtime_database_url="postgresql://test/runtime"),
                )
                with TestClient(app) as client:
                    assert client.get("/api/v1/health").status_code == 200
                    assert client.get("/admin").status_code == 404


def _collect_terminal_response(
    client: TestClient,
    agent_id: str,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
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

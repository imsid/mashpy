"""The events API: posting, dedup, the session queue, answers, cancel and streams."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.agents.companion import CompanionSpec
from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import Host, HostBuilder
from mash.sessions import event_request_id
from mash.testing.runtime_fixtures import (
    DelegatingLLMProvider,
    DeterministicLLMProvider,
    ToolCallingLLMProvider,
    build_spec,
    metadata,
    patch_companion_llm,
)
from mash.tools.ask_user import AskUserTool


@contextmanager
def _client(provider: Any = None, *, ask: bool = False, slow_seconds: float = 0.0):
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        stack.enter_context(
            patch.dict(os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""})
        )
        stack.enter_context(
            patch_companion_llm(provider or DeterministicLLMProvider(response_text="done"))
        )
        if ask:
            original = CompanionSpec.build_tools

            def _with_ask(self):
                tools = original(self)
                tools.register(AskUserTool())
                return tools

            stack.enter_context(patch.object(CompanionSpec, "build_tools", _with_ask))
        pool = (
            HostBuilder()
            .agent(
                build_spec(agent_id="fast", response_text="fast-ok", delay_seconds=slow_seconds),
                metadata=metadata(),
            )
            .host(Host(host_id="assistant", agents=("fast",)))
            .host(Host(host_id="other", agents=("fast",)))
            .build()
        )
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            client.put("/api/v1/policies/p-2", json={})
            yield client


def _post(client: TestClient, event_id: str, session_id: str = "s-1", **body: Any):
    return client.post(
        f"/api/v1/sessions/{session_id}/events",
        json={
            "event_id": event_id,
            "host_id": "assistant",
            "app_id": "app-1",
            "policy_id": "p-1",
            "message": f"message {event_id}",
            **body,
        },
    )


def _get(client: TestClient, event_id: str, session_id: str = "s-1") -> dict[str, Any]:
    resp = client.get(f"/api/v1/sessions/{session_id}/events/{event_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _wait(client: TestClient, event_id: str, statuses: set[str], session_id: str = "s-1"):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = _get(client, event_id, session_id)
        if state["status"] in statuses:
            return state
        time.sleep(0.03)
    raise AssertionError(f"{event_id} never reached {statuses}: {state}")


def _frames(client: TestClient, path: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    with client.stream("GET", path) as resp:
        assert resp.status_code == 200
        name = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:") and name:
                frames.append((name, json.loads(line[5:].strip())))
                if name in ("request.completed", "request.error", "request.cancelled", "event.cancelled"):
                    break
    return frames


def _companion_requests(client: TestClient, session_id: str = "s-1") -> list[dict]:
    resp = client.get(f"/api/v1/agent/companion/sessions/{session_id}/requests")
    return resp.json()["data"]["requests"]


def test_an_event_runs_as_a_request_with_a_derived_id_and_keeps_its_outcome() -> None:
    with _client() as client:
        resp = _post(client, "e-1")
        assert resp.status_code == 202, resp.text
        data = resp.json()["data"]
        assert data["request_id"] == event_request_id("s-1", "e-1")
        assert data["seq"] >= 1

        done = _wait(client, "e-1", {"completed"})
        assert done["response"]["text"] == "done"
        assert done["request_id"] == event_request_id("s-1", "e-1")
        assert done["app_id"] == "app-1"
        assert done["closed_at"] >= done["started_at"] >= done["received_at"]
        [record] = _companion_requests(client)
        assert record["request_id"] == done["request_id"]
        assert (record["app_id"], record["policy_id"]) == ("app-1", "p-1")


def test_a_seen_event_returns_its_row_and_runs_once() -> None:
    with _client() as client:
        first = _post(client, "e-1")
        assert first.status_code == 202
        for body in ({}, {"message": "something else"}, {"host_id": "nope", "policy_id": "nope"}):
            again = _post(client, "e-1", **body)
            assert again.status_code == 200, again.text
            assert again.json()["data"]["seq"] == first.json()["data"]["seq"]
        _wait(client, "e-1", {"completed"})
        assert _post(client, "e-1").status_code == 200
        assert len(_companion_requests(client)) == 1


def test_events_in_a_session_run_one_at_a_time_in_order() -> None:
    with _client(DeterministicLLMProvider(response_text="done", delay_seconds=0.3)) as client:
        for event_id in ("e-1", "e-2", "e-3"):
            assert _post(client, event_id).status_code == 202
        assert _get(client, "e-2")["status"] == "queued"
        assert _get(client, "e-3")["status"] == "queued"
        # Another session runs at once.
        assert _post(client, "x-1", session_id="s-2").status_code == 202
        _wait(client, "x-1", {"running", "completed"}, session_id="s-2")

        states = [_wait(client, e, {"completed"}) for e in ("e-1", "e-2", "e-3")]
        starts = [state["started_at"] for state in states]
        closes = [state["closed_at"] for state in states]
        # Each starts after the one before it closed.
        assert starts[1] >= closes[0] and starts[2] >= closes[1]
        ledger = client.get("/api/v1/sessions/s-1/events").json()["data"]["events"]
        assert [e["event_id"] for e in ledger] == ["e-1", "e-2", "e-3"]
        assert [e["seq"] for e in ledger] == sorted(e["seq"] for e in ledger)


def test_listings_filter_on_the_reported_status() -> None:
    with _client(DeterministicLLMProvider(response_text="done", delay_seconds=0.5)) as client:
        _post(client, "e-1")
        _post(client, "e-2")
        _post(client, "o-1", session_id="s-other", host_id="other")
        _wait(client, "e-1", {"running"})
        running = client.get(
            "/api/v1/hosts/assistant/events", params={"status": "running,waiting"}
        ).json()["data"]["events"]
        assert [e["event_id"] for e in running] == ["e-1"]
        queued = client.get(
            "/api/v1/sessions/s-1/events", params={"status": "queued"}
        ).json()["data"]["events"]
        assert [e["event_id"] for e in queued] == ["e-2"]
        assert client.get(
            "/api/v1/hosts/assistant/events", params={"status": "later"}
        ).status_code == 400
        _wait(client, "e-2", {"completed"})
        page = client.get(
            "/api/v1/hosts/assistant/events", params={"after_seq": 0, "limit": 1}
        ).json()["data"]["events"]
        assert [e["event_id"] for e in page] == ["e-1"]


def test_a_session_keeps_its_host_and_policy() -> None:
    with _client() as client:
        assert _post(client, "e-1").status_code == 202
        host = _post(client, "e-2", host_id="other")
        assert host.status_code == 409
        assert host.json()["error"]["code"] == "SESSION_HOST_MISMATCH"
        policy = _post(client, "e-3", policy_id="p-2")
        assert policy.status_code == 409
        assert policy.json()["error"]["code"] == "SESSION_POLICY_MISMATCH"
        # The app is not bound.
        assert _post(client, "e-4", app_id="app-2").status_code == 202
        assert _wait(client, "e-4", {"completed"})["app_id"] == "app-2"


def test_bad_events_are_refused_and_insert_nothing() -> None:
    with _client() as client:
        assert _post(client, "e-1", host_id="nope").json()["error"]["code"] == "HOST_NOT_FOUND"
        assert _post(client, "e-2", policy_id="nope").json()["error"]["code"] == "POLICY_NOT_FOUND"
        bad_schema = _post(client, "e-3", structured_output=["not", "a", "schema"])
        assert bad_schema.status_code == 400
        assert bad_schema.json()["error"]["code"] == "INVALID_STRUCTURED_OUTPUT"
        for body in ({"message": ""}, {"app_id": "has space"}, {"surprise": 1}):
            assert _post(client, "e-4", **body).status_code == 400, body
        bad_id = client.post("/api/v1/sessions/s-1/events", json={"event_id": "has space"})
        assert bad_id.status_code == 400
        assert client.get("/api/v1/sessions/s-1/events").json()["data"]["events"] == []


def test_an_answer_goes_to_the_events_request_and_never_inserts() -> None:
    provider = ToolCallingLLMProvider(tool_name="AskUser", arguments={"question": "Which one?"})
    with _client(provider, ask=True) as client:
        _post(client, "e-1")
        waiting = _wait(client, "e-1", {"waiting"})
        [interaction] = waiting["interactions"]
        interaction_id = interaction["interaction_id"]
        assert interaction["prompt"] == "Which one?"

        mixed = client.post(
            "/api/v1/sessions/s-1/events",
            json={"event_id": "e-1", "interaction_id": interaction_id, "response": "x", "message": "hi"},
        )
        assert mixed.status_code == 400
        unknown = client.post(
            "/api/v1/sessions/s-1/events",
            json={"event_id": "e-1", "interaction_id": "itr_nope", "response": "x"},
        )
        assert unknown.json()["error"]["code"] == "INTERACTION_NOT_FOUND"
        missing = client.post(
            "/api/v1/sessions/s-1/events",
            json={"event_id": "e-9", "interaction_id": interaction_id, "response": "x"},
        )
        assert missing.json()["error"]["code"] == "EVENT_NOT_FOUND"

        answer = {"event_id": "e-1", "interaction_id": interaction_id, "response": "the blue one"}
        resp = client.post("/api/v1/sessions/s-1/events", json=answer)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["interaction_id"] == interaction_id
        assert _wait(client, "e-1", {"completed"})["response"]["text"] == "the blue one"
        # The same answer again is fine; another is refused.
        assert client.post("/api/v1/sessions/s-1/events", json=answer).status_code == 200
        other = client.post("/api/v1/sessions/s-1/events", json={**answer, "response": "red"})
        assert other.status_code == 409
        assert other.json()["error"]["code"] == "INTERACTION_CLOSED"
        assert [e["event_id"] for e in client.get("/api/v1/sessions/s-1/events").json()["data"]["events"]] == ["e-1"]


def test_a_message_while_an_event_waits_queues_behind_it() -> None:
    provider = ToolCallingLLMProvider(tool_name="AskUser", arguments={"question": "Which one?"})
    with _client(provider, ask=True) as client:
        _post(client, "e-1")
        waiting = _wait(client, "e-1", {"waiting"})
        assert _post(client, "e-2").status_code == 202
        assert _get(client, "e-2")["status"] == "queued"
        interaction_id = waiting["interactions"][0]["interaction_id"]
        client.post(
            "/api/v1/sessions/s-1/events",
            json={"event_id": "e-1", "interaction_id": interaction_id, "response": "blue"},
        )
        _wait(client, "e-1", {"completed"})
        _wait(client, "e-2", {"waiting", "running", "completed"})


def test_cancel_a_queued_event_a_started_one_and_a_finished_one() -> None:
    provider = ToolCallingLLMProvider(tool_name="AskUser", arguments={"question": "Which one?"})
    with _client(provider, ask=True) as client:
        _post(client, "e-1")
        _wait(client, "e-1", {"waiting"})
        _post(client, "e-2")
        cancelled = client.post("/api/v1/sessions/s-1/events/e-2/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"
        assert cancelled.json()["data"]["request_id"] is None
        assert _frames(client, "/api/v1/sessions/s-1/events/e-2/stream")[-1][0] == "event.cancelled"

        assert client.post("/api/v1/sessions/s-1/events/e-1/cancel").status_code == 200
        _wait(client, "e-1", {"cancelled"})
        again = client.post("/api/v1/sessions/s-1/events/e-1/cancel")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "EVENT_TERMINAL"
        assert client.post("/api/v1/sessions/s-1/events/nope/cancel").status_code == 404


def test_a_stream_opened_on_a_queued_event_relays_its_request_from_the_start() -> None:
    with _client(DeterministicLLMProvider(response_text="done", delay_seconds=0.4)) as client:
        _post(client, "e-1")
        _post(client, "e-2")
        frames = _frames(client, "/api/v1/sessions/s-1/events/e-2/stream")
        names = [name for name, _ in frames]
        assert names[0] == "event.queued"
        assert names[1] == "request.accepted"
        assert names[-1] == "request.completed"
        assert frames[-1][1]["response"]["text"] == "done"


def test_a_reconnected_stream_misses_no_frame_and_repeats_none() -> None:
    with _client() as client:
        _post(client, "e-1")
        _wait(client, "e-1", {"completed"})
        seqs: list[int] = []
        with client.stream("GET", "/api/v1/sessions/s-1/events/e-1/stream") as resp:
            for line in resp.iter_lines():
                if line.startswith("id:"):
                    seqs.append(int(line[3:].strip()))
        cut = seqs[len(seqs) // 2]
        rest: list[int] = []
        with client.stream(
            "GET", "/api/v1/sessions/s-1/events/e-1/stream", params={"after": cut}
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("id:"):
                    rest.append(int(line[3:].strip()))
        assert rest == [seq for seq in seqs if seq > cut]


def test_cancelling_an_event_cancels_its_requests_children() -> None:
    provider = DelegatingLLMProvider(final_text="done", subagent_id="fast", subagent_prompt="go")
    with _client(provider, slow_seconds=5.0) as client:
        _post(client, "e-1")
        request_id = event_request_id("s-1", "e-1")
        deadline = time.monotonic() + 10
        children: list[dict] = []
        while time.monotonic() < deadline and not children:
            store = client.app.state.runtime_state.pool.get_runtime_store()
            children = client.portal.call(store.list_child_requests, request_id)
            time.sleep(0.03)
        [child] = children
        assert child["status"] == "running"

        assert client.post("/api/v1/sessions/s-1/events/e-1/cancel").status_code == 200
        assert _wait(client, "e-1", {"cancelled"})["status"] == "cancelled"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            [child] = client.portal.call(store.list_child_requests, request_id)
            if child["status"] != "running":
                break
            time.sleep(0.03)
        assert child["status"] == "cancelled"

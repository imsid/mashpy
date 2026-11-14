"""Tests for remote host HTTP client behavior."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

import httpx

from mash.cli import client as client_module
from mash.cli.client import (
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STREAM_TIMEOUT,
    MashHostClient,
    MashHostClientError,
)

SSE_COMPLETED = 'event: request.completed\ndata: {"response": {"text": "ok"}}\n\n'


class _Recorder:
    """A transport handler that records each request and replays queued replies.

    A reply is a JSON payload (dict), an SSE body (str), an ``httpx.Response``
    or an exception to raise. With the queue empty it answers a stream with
    one ``request.completed`` frame and anything else with a request id.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": request.headers,
                "json": json.loads(request.content) if request.content else None,
                "timeout": request.extensions.get("timeout"),
            }
        )
        if self.responses:
            reply = self.responses.pop(0)
        elif request.url.path.endswith("/events"):
            reply = SSE_COMPLETED
        else:
            reply = {"data": {"request_id": "req-1"}}
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, str):
            return httpx.Response(
                200, text=reply, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=reply)


def _client(*replies: Any, api_key: str | None = None) -> tuple[MashHostClient, _Recorder]:
    recorder = _Recorder()
    recorder.responses.extend(replies)
    transport = httpx.MockTransport(recorder)
    client = MashHostClient(
        "http://localhost:8000",
        api_key=api_key,
        transport=transport,
        async_transport=transport,
    )
    return client, recorder


def _timeout(value: httpx.Timeout) -> dict[str, Any]:
    return value.as_dict()


async def _collect(stream) -> list[dict[str, Any]]:
    return [event async for event in stream]


class MashHostClientTests(unittest.TestCase):
    def test_stream_request_uses_long_lived_stream_timeout(self) -> None:
        client, session = _client()

        events = list(client.stream_request("primary", "req-1"))

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(session.calls[-1]["timeout"], _timeout(DEFAULT_STREAM_TIMEOUT))
        self.assertIsNone(session.calls[-1]["timeout"]["read"])

    def test_stream_request_flushes_final_event_without_trailing_blank(self) -> None:
        # The server closes right after the terminal event. The parser must
        # flush the buffered event at stream end, else the caller never sees
        # request.completed.
        client, _ = _client(
            'event: agent.trace\ndata: {"event_type": "x"}\n\n'
            'event: request.completed\ndata: {"status": "completed"}'
        )

        events = list(client.stream_request("primary", "req-1"))

        self.assertEqual(
            [e["event"] for e in events], ["agent.trace", "request.completed"]
        )
        self.assertEqual(events[-1]["data"]["status"], "completed")

    def test_stream_request_does_not_double_emit_on_trailing_blank(self) -> None:
        # A normal stream that does end with a blank line must not yield the
        # final event twice (the end-of-stream flush is a no-op there).
        client, _ = _client()

        events = list(client.stream_request("primary", "req-1"))

        self.assertEqual([e["event"] for e in events], ["request.completed"])

    def test_stream_request_reads_each_frames_id_as_seq(self) -> None:
        client, session = _client(
            'id: 4\nevent: agent.trace\ndata: {}\n\n'
            ': keep-alive\n\n'
            'id: 5\nevent: request.completed\ndata: {}\n\n'
        )

        events = list(client.stream_request("primary", "req-1", after=3))

        self.assertEqual([e["seq"] for e in events], [4, 5])
        self.assertTrue(session.calls[-1]["url"].endswith("/events?after=3"))

    def test_non_stream_requests_keep_default_timeout(self) -> None:
        client, session = _client()

        client.submit_request("primary", message="hello", session_id="s-1", user_id="u-1")

        self.assertEqual(session.calls[-1]["timeout"], _timeout(DEFAULT_REQUEST_TIMEOUT))

    def test_the_api_key_is_sent_as_a_bearer_token(self) -> None:
        client, session = _client(api_key="k")
        client.health()
        self.assertEqual(session.calls[-1]["headers"]["authorization"], "Bearer k")

    def test_an_error_status_raises_with_the_hosts_message(self) -> None:
        client, _ = _client(
            httpx.Response(409, json={"error": {"message": "agent is disabled"}})
        )
        with self.assertRaises(MashHostClientError) as raised:
            client.get_agent("primary")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            str(raised.exception),
            "GET /api/v1/agent/primary failed (409): agent is disabled",
        )

    def test_an_error_carries_the_apis_code_when_there_is_one(self) -> None:
        client, _ = _client(
            httpx.Response(
                404, json={"error": {"code": "CORTEX_NOT_FOUND", "message": "user not found"}}
            ),
            httpx.Response(404, json={"detail": "Not Found"}),
            httpx.Response(502, text="bad gateway"),
        )
        with self.assertRaises(MashHostClientError) as raised:
            client.get_cortex_user("u-1")
        self.assertEqual(raised.exception.code, "CORTEX_NOT_FOUND")
        for _ in range(2):
            with self.assertRaises(MashHostClientError) as raised:
                client.get_cortex_user("u-1")
            self.assertIsNone(raised.exception.code)

    def test_a_host_that_cannot_be_reached_raises_a_client_error(self) -> None:
        client, _ = _client(httpx.ConnectError("connection refused"))
        with self.assertRaises(MashHostClientError) as raised:
            client.health()
        self.assertIsNone(raised.exception.status_code)
        self.assertIn("connection refused", str(raised.exception))

    def test_a_stream_with_an_error_status_raises_with_the_hosts_message(self) -> None:
        client, _ = _client(
            httpx.Response(404, json={"error": {"message": "Request not found"}})
        )
        with self.assertRaises(MashHostClientError) as raised:
            list(client.stream_request("primary", "missing"))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("Request not found", str(raised.exception))


class RequestEventsTests(unittest.TestCase):
    """``request_events``: the async stream that continues after a drop."""

    def setUp(self) -> None:
        self._delay = client_module.STREAM_RETRY_DELAY
        client_module.STREAM_RETRY_DELAY = 0.0

    def tearDown(self) -> None:
        client_module.STREAM_RETRY_DELAY = self._delay

    def test_it_yields_the_events_to_the_terminal_one(self) -> None:
        client, session = _client(
            'id: 1\nevent: request.accepted\ndata: {}\n\n'
            'id: 2\nevent: request.completed\ndata: {}\n\n'
        )
        events = asyncio.run(_collect(client.request_events("primary", "r-1")))
        self.assertEqual(
            [(e["seq"], e["event"]) for e in events],
            [(1, "request.accepted"), (2, "request.completed")],
        )
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0]["url"].endswith("/request/r-1/events"))

    def test_a_stream_that_ends_early_continues_after_the_last_seq(self) -> None:
        client, session = _client(
            'id: 1\nevent: request.accepted\ndata: {}\n\n'
            'id: 2\nevent: agent.trace\ndata: {"n": 2}\n\n',
            'id: 3\nevent: agent.trace\ndata: {"n": 3}\n\n'
            'id: 4\nevent: request.completed\ndata: {}\n\n',
        )
        events = asyncio.run(_collect(client.request_events("primary", "r-1")))
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])
        self.assertTrue(session.calls[1]["url"].endswith("/events?after=2"))

    def test_a_dropped_connection_is_retried_from_the_cursor(self) -> None:
        client, session = _client(
            httpx.ReadError("connection reset"),
            'id: 6\nevent: request.completed\ndata: {}\n\n',
        )
        events = asyncio.run(
            _collect(client.request_events("primary", "r-1", after=5))
        )
        self.assertEqual([e["seq"] for e in events], [6])
        self.assertTrue(
            all(call["url"].endswith("/events?after=5") for call in session.calls)
        )

    def test_it_gives_up_after_the_retries_run_out(self) -> None:
        client, session = _client(
            *[httpx.ConnectError("refused")] * (client_module.STREAM_RETRIES + 1)
        )
        with self.assertRaises(MashHostClientError) as raised:
            asyncio.run(_collect(client.request_events("primary", "r-1")))
        self.assertIn("refused", str(raised.exception))
        self.assertEqual(len(session.calls), client_module.STREAM_RETRIES + 1)

    def test_an_event_resets_the_retry_count(self) -> None:
        # More drops than STREAM_RETRIES in total, never that many in a row.
        replies: list[Any] = []
        for seq in range(1, client_module.STREAM_RETRIES + 3):
            replies.append(httpx.ReadError("reset"))
            replies.append(f"id: {seq}\nevent: agent.trace\ndata: {{}}\n\n")
        last = client_module.STREAM_RETRIES + 3
        replies.append(f"id: {last}\nevent: request.completed\ndata: {{}}\n\n")
        client, _ = _client(*replies)
        events = asyncio.run(_collect(client.request_events("primary", "r-1")))
        self.assertEqual([e["seq"] for e in events], list(range(1, last + 1)))

    def test_an_error_status_is_not_retried(self) -> None:
        client, session = _client(
            httpx.Response(404, json={"error": {"message": "Request not found"}})
        )
        with self.assertRaises(MashHostClientError) as raised:
            asyncio.run(_collect(client.request_events("primary", "missing")))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(session.calls), 1)

    def test_a_host_without_seqs_is_read_once_and_not_continued(self) -> None:
        client, session = _client('event: agent.trace\ndata: {}\n\n')
        events = asyncio.run(_collect(client.request_events("primary", "r-1")))
        self.assertEqual([e["event"] for e in events], ["agent.trace"])
        self.assertEqual(len(session.calls), 1)

    def test_workflow_run_events_reads_the_runs_stream(self) -> None:
        client, session = _client(
            'event: step.started\ndata: {"step_id": "scan"}\n\n'
            'event: workflow.completed\ndata: {"result": {}}\n\n'
        )
        events = asyncio.run(_collect(client.workflow_run_events("wf/one", "run/1")))
        self.assertEqual(
            [e["event"] for e in events], ["step.started", "workflow.completed"]
        )
        self.assertIn(
            "/api/v1/workflow/wf%2Fone/runs/run%2F1/events", session.calls[0]["url"]
        )


class MashHostClientRouteTests(unittest.TestCase):
    def _client(self, *payloads) -> tuple[MashHostClient, _Recorder]:
        return _client(*payloads, api_key="k")

    def test_get_reasoning_trace_uses_agent_trace_route(self) -> None:
        client, session = self._client()

        client.get_reasoning_trace("primary", "s-1", "trace-1")

        self.assertIn(
            "/api/v1/agent/primary/session/s-1/trace/trace-1/reasoning",
            str(session.calls[-1]["url"]),
        )

    def test_submit_sends_mode_only_when_given(self) -> None:
        client, session = self._client()
        client.submit_request("primary", message="hi", session_id="s-1", user_id="u-1")
        client.submit_request("primary", message="hi", session_id="s-1", user_id="u-1", mode="bg")
        self.assertNotIn("mode", session.calls[0]["json"])
        self.assertEqual(session.calls[1]["json"]["mode"], "bg")

        client, session = self._client({"data": {"request_id": "r", "agent_id": "primary"}})
        client.submit_host_request(
            "assistant", message="hi", session_id="s-1", user_id="u-1", mode="bg"
        )
        self.assertIn("/api/v1/hosts/assistant/request", session.calls[0]["url"])
        self.assertEqual(session.calls[0]["json"]["mode"], "bg")

    def test_set_request_mode_posts_to_the_mode_route(self) -> None:
        client, session = self._client({"data": {"request_id": "r-1", "mode": "bg"}})
        self.assertEqual(client.set_request_mode("primary", "r-1", "bg")["mode"], "bg")
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/api/v1/agent/primary/request/r-1/mode"))
        self.assertEqual(call["json"], {"mode": "bg"})

    def test_cancel_request_posts_to_the_cancel_route(self) -> None:
        client, session = self._client({"data": {"status": "cancelled"}})
        client.cancel_request("research", "r-2")
        self.assertTrue(
            session.calls[0]["url"].endswith("/api/v1/agent/research/request/r-2/cancel")
        )

    def test_list_session_requests_passes_its_filters(self) -> None:
        client, session = self._client({"data": {"requests": [{"request_id": "r-1"}, "junk"]}})
        records = client.list_session_requests(
            "primary", "s-1", mode="bg", status="running", started_by="user"
        )
        self.assertEqual(records, [{"request_id": "r-1"}])
        self.assertEqual(
            session.calls[0]["url"],
            "http://localhost:8000/api/v1/agent/primary/sessions/s-1/requests"
            "?mode=bg&status=running&started_by=user",
        )

    def test_submit_feedback_posts_session_context(self) -> None:
        client, session = self._client(
            {"data": {"feedback": {"feedback_id": 1}}}
        )

        result = client.submit_feedback(
            "primary",
            message="trace output is hard to read",
            host_id="assistant",
            session_id="s-1",
            request_id="r-9",
        )

        self.assertEqual(result["feedback"]["feedback_id"], 1)
        self.assertEqual(session.calls[-1]["method"], "POST")
        self.assertEqual(session.calls[-1]["url"], "http://localhost:8000/api/v1/feedback")
        body = session.calls[-1]["json"]
        self.assertEqual(body["agent_id"], "primary")
        self.assertEqual(body["message"], "trace output is hard to read")
        self.assertEqual(body["feedback_type"], "text")
        self.assertEqual(body["host_id"], "assistant")
        self.assertEqual(body["session_id"], "s-1")
        self.assertEqual(body["request_id"], "r-9")

    def test_list_feedback_uses_feedback_route_with_after(self) -> None:
        client, session = self._client(
            {"data": {"feedback": [{"feedback_id": 2, "message": "hi"}]}}
        )

        feedback = client.list_feedback("primary", after=1_700_000_000.0, q="trace", limit=10)

        self.assertEqual(feedback[0]["feedback_id"], 2)
        self.assertEqual(session.calls[-1]["method"], "GET")
        url = str(session.calls[-1]["url"])
        self.assertIn("/api/v1/feedback?", url)
        self.assertIn("agent_id=primary", url)
        self.assertIn("after=1700000000.0", url)
        self.assertIn("q=trace", url)

    def test_list_workflows_uses_workflow_route(self) -> None:
        client, session = self._client(
            {
                    "data": {
                        "workflows": [
                            {
                                "workflow_id": "changelog",
                                "mode": "pipeline",
                                "step_count": 1,
                                "step_preview": [
                                    {
                                        "ordinal": 0,
                                        "step_id": "scan",
                                        "kind": "agent",
                                        "agent_id": "worker",
                                    }
                                ],
                            }
                        ]
                    }
                }
        )

        workflows = client.list_workflows()

        self.assertEqual(workflows[0]["workflow_id"], "changelog")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertEqual(session.calls[-1]["url"], "http://localhost:8000/api/v1/workflow")

    def test_get_workflow_definition_uses_quoted_route(self) -> None:
        client, session = self._client(
            {
                    "data": {
                        "workflow_id": "wf/one",
                        "mode": "pipeline",
                        "steps": [],
                    }
                }
        )

        definition = client.get_workflow_definition("wf/one")

        self.assertEqual(definition["mode"], "pipeline")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertIn(
            "/api/v1/workflow/wf%2Fone",
            str(session.calls[-1]["url"]),
        )

    def test_rerun_and_compact_post_to_their_routes(self) -> None:
        client, session = self._client(
            {"data": {"request_id": "r-2", "agent_id": "primary", "mode": "fg"}},
            {"data": {"summary_text": "so far", "trace_id": "t-1"}},
        )
        self.assertEqual(client.rerun_request("primary", "r/1")["request_id"], "r-2")
        self.assertEqual(client.compact_session("primary", "s 1")["summary_text"], "so far")
        rerun, compact = session.calls
        self.assertEqual(rerun["method"], "POST")
        self.assertIn("/api/v1/agent/primary/request/r%2F1/rerun", rerun["url"])
        self.assertEqual(compact["method"], "POST")
        self.assertIn("/api/v1/agent/primary/sessions/s%201/compact", compact["url"])
        self.assertEqual(compact["json"], {"reason": "manual"})

    def test_get_usage_sends_only_the_filters_given(self) -> None:
        client, session = self._client({"data": {"buckets": [{"input_tokens": 5}]}})
        self.assertEqual(
            client.get_usage("primary", from_ts=100.0), [{"input_tokens": 5}]
        )
        url = str(session.calls[0]["url"])
        self.assertIn("/api/v1/telemetry/usage", url)
        self.assertIn("agent_id=primary", url)
        self.assertIn("from_ts=100.0", url)
        self.assertIn("bucket=day", url)
        self.assertNotIn("session_id", url)

    def test_tools_and_skills_read_the_pool_routes(self) -> None:
        client, session = self._client(
            {"data": {"tools": [{"tool": {"name": "t"}, "agents": ["a"]}]}},
            {"data": {"skills": []}},
        )
        self.assertEqual(client.list_tools()[0]["tool"]["name"], "t")
        self.assertEqual(client.list_skills(), [])
        self.assertTrue(str(session.calls[0]["url"]).endswith("/api/v1/tools"))
        self.assertTrue(str(session.calls[1]["url"]).endswith("/api/v1/skills"))

    def test_cortex_calls_use_the_users_routes(self) -> None:
        client, session = self._client(
            {"data": {"user_id": "u@1", "profile": "p", "entry_count": 1}},
            {"data": {"entries": [{"entry_id": "e-1"}], "next": None}},
            {"data": {"entry": {"entry_id": "e-2"}, "version": 3}},
            {"data": {"deleted": "e-2", "version": 4}},
        )
        self.assertEqual(client.get_cortex_user("u@1")["profile"], "p")
        self.assertEqual(client.list_cortex_entries("u@1", q="tea", limit=5), [{"entry_id": "e-1"}])
        self.assertEqual(
            client.create_cortex_entry("u@1", topic="note", content="likes tea")["entry_id"], "e-2"
        )
        client.delete_cortex_entry("u@1", "e-2")
        user, entries, create, delete = session.calls
        self.assertTrue(str(user["url"]).endswith("/api/v1/cortex/users/u%401"))
        self.assertIn("/api/v1/cortex/users/u%401/entries", str(entries["url"]))
        self.assertIn("q=tea", str(entries["url"]))
        self.assertIn("limit=5", str(entries["url"]))
        self.assertEqual(create["method"], "POST")
        self.assertEqual(create["json"], {"topic": "note", "content": "likes tea"})
        self.assertEqual(delete["method"], "DELETE")
        self.assertTrue(str(delete["url"]).endswith("/api/v1/cortex/users/u%401/entries/e-2"))

    def test_browser_calls_use_the_users_routes(self) -> None:
        client, session = self._client(
            {"data": {"user_id": "u-1", "sites": [{"domain": "pge.com", "cookies": 2}]}},
            {"data": {"user_id": "u-1", "domain": "pge.com", "cookies": 2}},
        )
        self.assertEqual(client.list_browser_sites("u-1")[0]["domain"], "pge.com")
        self.assertEqual(client.clear_browser_site("u-1", "pge.com")["cookies"], 2)
        sites, clear = session.calls
        self.assertTrue(str(sites["url"]).endswith("/api/v1/browser/users/u-1/sites"))
        self.assertEqual(clear["method"], "DELETE")
        self.assertTrue(str(clear["url"]).endswith("/api/v1/browser/users/u-1/sites/pge.com"))

    def test_list_sessions_sends_the_user_and_limit_when_given(self) -> None:
        client, session = self._client({"data": {"sessions": []}}, {"data": {"sessions": []}})
        client.list_sessions("primary")
        client.list_sessions("primary", user_id="u-1", limit=20)
        self.assertNotIn("?", str(session.calls[0]["url"]))
        self.assertIn("user_id=u-1", str(session.calls[1]["url"]))
        self.assertIn("limit=20", str(session.calls[1]["url"]))

    def test_list_workflow_runs_reads_the_runs_route(self) -> None:
        client, session = self._client(
            {"data": {"runs": [{"run_id": "run-1", "dedup_key": "r-1-0-0"}]}}
        )

        runs = client.list_workflow_runs("wf/one", limit=10)

        self.assertEqual(runs, [{"run_id": "run-1", "dedup_key": "r-1-0-0"}])
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertIn("/api/v1/workflow/wf%2Fone/runs", str(session.calls[-1]["url"]))
        self.assertIn("limit=10", str(session.calls[-1]["url"]))

    def test_run_workflow_posts_optional_dedup_key(self) -> None:
        client, session = self._client(
            {
                    "data": {
                        "run_id": "run-1",
                        "workflow_id": "wf/one",
                        "status": "queued",
                    }
                }
        )

        run = client.run_workflow("wf/one", dedup_key="manual")

        self.assertEqual(run["run_id"], "run-1")
        self.assertEqual(session.calls[-1]["method"], "POST")
        self.assertIn("/api/v1/workflow/wf%2Fone/run", str(session.calls[-1]["url"]))
        self.assertEqual(session.calls[-1]["json"], {"dedup_key": "manual"})

    def test_run_workflow_omits_missing_dedup_key(self) -> None:
        client, session = self._client()

        client.run_workflow("wf")

        self.assertEqual(session.calls[-1]["json"], {})

    def test_run_workflow_posts_input_object(self) -> None:
        client, session = self._client()

        client.run_workflow("wf", workflow_input={"x": 1})

        self.assertEqual(session.calls[-1]["json"], {"input": {"x": 1}})

    def test_get_workflow_run_uses_quoted_route(self) -> None:
        client, session = self._client(
            {
                    "data": {
                        "run_id": "run/1",
                        "workflow_id": "wf",
                        "status": "completed",
                    }
                }
        )

        run = client.get_workflow_run("wf", "run/1")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertIn(
            "/api/v1/workflow/wf/runs/run%2F1",
            str(session.calls[-1]["url"]),
        )

    def test_stream_workflow_run_uses_events_route_and_stream_timeout(self) -> None:
        client, session = self._client()

        events = list(client.stream_workflow_run("wf/one", "run/1"))

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertEqual(session.calls[-1]["timeout"], _timeout(DEFAULT_STREAM_TIMEOUT))
        self.assertIn(
            "/api/v1/workflow/wf%2Fone/runs/run%2F1/events",
            str(session.calls[-1]["url"]),
        )


if __name__ == "__main__":
    unittest.main()

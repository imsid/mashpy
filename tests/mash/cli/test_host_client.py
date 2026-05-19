"""Tests for remote host HTTP client behavior."""

from __future__ import annotations

import unittest

from mash.cli.client import DEFAULT_REQUEST_TIMEOUT, DEFAULT_STREAM_TIMEOUT, MashHostClient


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload=None) -> None:
        self._payload = payload or {"data": {"request_id": "req-1"}}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def iter_lines(self, chunk_size=1, decode_unicode=True):
        del chunk_size, decode_unicode
        yield "event: request.completed"
        yield 'data: {"response": {"text": "ok"}}'
        yield ""

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: list[_FakeResponse] = []

    def request(self, method, url, headers=None, json=None, stream=False, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "stream": stream,
                "timeout": timeout,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse()

    def close(self) -> None:
        return None


class MashHostClientTests(unittest.TestCase):
    def test_stream_request_uses_long_lived_stream_timeout(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        client._session = session  # type: ignore[assignment]

        events = list(client.stream_request("primary", "req-1"))

        self.assertEqual(events[-1]["event"], "request.completed")
        self.assertEqual(session.calls[-1]["timeout"], DEFAULT_STREAM_TIMEOUT)

    def test_non_stream_requests_keep_default_timeout(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        client._session = session  # type: ignore[assignment]

        client.submit_request("primary", message="hello", session_id="s-1")

        self.assertEqual(session.calls[-1]["timeout"], DEFAULT_REQUEST_TIMEOUT)

    def test_get_reasoning_trace_uses_telemetry_route(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        client._session = session  # type: ignore[assignment]

        client.get_reasoning_trace("primary", "s-1", "trace-1")

        self.assertIn(
            "/api/v1/telemetry/reasoning-trace?agent_id=primary&session_id=s-1&trace_id=trace-1",
            str(session.calls[-1]["url"]),
        )

    def test_list_workflows_uses_workflow_route(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        session.responses.append(
            _FakeResponse(
                {
                    "data": {
                        "workflows": [
                            {
                                "workflow_id": "changelog",
                                "tasks": [{"task_id": "scan", "agent_id": "worker"}],
                            }
                        ]
                    }
                }
            )
        )
        client._session = session  # type: ignore[assignment]

        workflows = client.list_workflows()

        self.assertEqual(workflows[0]["workflow_id"], "changelog")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertEqual(session.calls[-1]["url"], "http://localhost:8000/api/v1/workflows")

    def test_run_workflow_posts_optional_dedup_key(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        session.responses.append(
            _FakeResponse(
                {
                    "data": {
                        "run_id": "run-1",
                        "workflow_id": "wf/one",
                        "status": "queued",
                    }
                }
            )
        )
        client._session = session  # type: ignore[assignment]

        run = client.run_workflow("wf/one", dedup_key="manual")

        self.assertEqual(run["run_id"], "run-1")
        self.assertEqual(session.calls[-1]["method"], "POST")
        self.assertIn("/api/v1/workflows/wf%2Fone/run", str(session.calls[-1]["url"]))
        self.assertEqual(session.calls[-1]["json"], {"dedup_key": "manual"})

    def test_run_workflow_omits_missing_dedup_key(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        client._session = session  # type: ignore[assignment]

        client.run_workflow("wf")

        self.assertEqual(session.calls[-1]["json"], {})

    def test_run_workflow_posts_input_object(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        client._session = session  # type: ignore[assignment]

        client.run_workflow("wf", workflow_input={"x": 1})

        self.assertEqual(session.calls[-1]["json"], {"input": {"x": 1}})

    def test_get_workflow_run_uses_quoted_route(self) -> None:
        client = MashHostClient("http://localhost:8000")
        session = _RecordingSession()
        session.responses.append(
            _FakeResponse(
                {
                    "data": {
                        "run_id": "run/1",
                        "workflow_id": "wf",
                        "status": "completed",
                    }
                }
            )
        )
        client._session = session  # type: ignore[assignment]

        run = client.get_workflow_run("wf", "run/1")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(session.calls[-1]["method"], "GET")
        self.assertIn(
            "/api/v1/workflows/wf/runs/run%2F1",
            str(session.calls[-1]["url"]),
        )


if __name__ == "__main__":
    unittest.main()

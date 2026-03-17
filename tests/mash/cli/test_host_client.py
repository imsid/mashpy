"""Tests for remote host HTTP client behavior."""

from __future__ import annotations

import unittest

from mash.cli.client import DEFAULT_REQUEST_TIMEOUT, DEFAULT_STREAM_TIMEOUT, MashHostClient


class _FakeResponse:
    status_code = 200
    text = ""

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
        return {"data": {"request_id": "req-1"}}


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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

        client.submit_request("primary", message="hello")

        self.assertEqual(session.calls[-1]["timeout"], DEFAULT_REQUEST_TIMEOUT)


if __name__ == "__main__":
    unittest.main()

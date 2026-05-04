"""Tests for InvokeSubagent tool."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, AsyncIterator, Dict, Optional

from mash.logging import clear_trace_id, set_trace_id
from mash.tools.subagent import InvokeSubagentTool
from mash.tools.subagent import derive_subagent_session_id


class _FakeClient:
    def __init__(self) -> None:
        self.last_call: Optional[Dict[str, Any]] = None
        self._error: Optional[Exception] = None
        self._request_id = "r1"
        self._events: list[Dict[str, Any]] = [
            {"event": "request.accepted", "data": {"request_id": "r1", "status": "accepted"}},
            {"event": "request.started", "data": {"request_id": "r1", "status": "started"}},
            {
                "event": "request.completed",
                "data": {
                    "request_id": "r1",
                    "status": "completed",
                    "response": {"text": "client-ok", "metadata": {"source": "client"}},
                },
            },
        ]

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
    ) -> str:
        self.last_call = {
            "message": message,
            "session_id": session_id,
            "request_kind": "plain",
        }
        return self._request_id

    async def post_subagent_request(
        self,
        message: str,
        *,
        session_id: str,
        primary_session_id: str,
        primary_app_id: str,
        subagent_id: str,
        subagent_invoke_opts: Dict[str, Any],
    ) -> str:
        self.last_call = {
            "message": message,
            "session_id": session_id,
            "request_kind": "subagent",
            "primary_session_id": primary_session_id,
            "primary_app_id": primary_app_id,
            "subagent_id": subagent_id,
            "subagent_invoke_opts": subagent_invoke_opts,
        }
        return self._request_id

    async def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        if self.last_call is not None:
            self.last_call["timeout"] = timeout
            self.last_call["request_id"] = request_id
        if self._error:
            raise self._error
        for event in self._events:
            await asyncio.sleep(0)
            yield event


class _RecordingEventLogger:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class InvokeSubagentToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_trace_id()
        self.client = _FakeClient()
        self.event_logger = _RecordingEventLogger()
        self.tool = InvokeSubagentTool(
            client_resolver=lambda _agent_id: self.client,
            primary_app_id="primary-app",
            primary_session_id="s1",
            event_logger=self.event_logger,
        )

    def tearDown(self) -> None:
        clear_trace_id()

    async def test_success_returns_json_payload(self) -> None:
        set_trace_id("trace-primary")
        result = await self.tool.execute(
            {"agent_id": "research", "prompt": "Summarize issue", "opts": {"x": 1}}
        )
        self.assertFalse(result.is_error)
        expected_subagent_session_id = derive_subagent_session_id(
            "primary-app",
            "s1",
            "research",
        )
        payload = json.loads(result.content)
        self.assertEqual(payload["agent_id"], "research")
        self.assertEqual(payload["primary_session_id"], "s1")
        self.assertEqual(payload["subagent_session_id"], expected_subagent_session_id)
        self.assertEqual(payload["primary_app_id"], "primary-app")
        self.assertEqual(payload["request_id"], "r1")
        self.assertEqual(payload["text"], "client-ok")
        self.assertEqual(result.metadata["subagent_session_id"], expected_subagent_session_id)
        assert self.client.last_call is not None
        self.assertEqual(self.client.last_call["session_id"], expected_subagent_session_id)
        self.assertEqual(self.client.last_call["request_kind"], "subagent")
        self.assertEqual(self.client.last_call["primary_app_id"], "primary-app")
        self.assertEqual(self.client.last_call["subagent_invoke_opts"]["x"], 1)
        self.assertEqual(self.client.last_call["timeout"], 360.0)
        streamed_event_types = [event.event_type for event in self.event_logger.events]
        self.assertEqual(
            streamed_event_types,
            ["subagent.request.accepted", "subagent.request.started", "subagent.request.completed"],
        )
        self.assertEqual(
            self.event_logger.events[0].payload["subagent_session_id"],
            expected_subagent_session_id,
        )
        self.assertEqual(
            self.event_logger.events[0].payload["primary_session_id"],
            "s1",
        )

    async def test_success_without_active_trace_skips_stream_event_logging(self) -> None:
        result = await self.tool.execute(
            {"agent_id": "research", "prompt": "Summarize issue", "opts": {"x": 1}}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.event_logger.events, [])

    async def test_error_returns_error_result(self) -> None:
        self.client._error = TimeoutError("timed out")
        result = await self.tool.execute({"agent_id": "research", "prompt": "hello"})
        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["agent_id"], "research")
        self.assertEqual(payload["primary_session_id"], "s1")
        self.assertEqual(payload["error_source"], "timeout")
        self.assertIn("timed out", payload["error"])

    async def test_request_error_returns_structured_payload(self) -> None:
        self.client._events = [
            {"event": "request.accepted", "data": {"request_id": "r1", "status": "accepted"}},
            {"event": "request.started", "data": {"request_id": "r1", "status": "started"}},
            {
                "event": "request.error",
                "data": {
                    "request_id": "r1",
                    "status": "error",
                    "error": "Error code: 400 - {'error': {'code': 'context_length_exceeded'}}",
                    "error_code": "context_length_exceeded",
                    "retryable": False,
                },
            },
        ]

        result = await self.tool.execute({"agent_id": "research", "prompt": "hello"})

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["agent_id"], "research")
        self.assertEqual(payload["primary_session_id"], "s1")
        self.assertEqual(payload["request_id"], "r1")
        self.assertEqual(payload["error_code"], "context_length_exceeded")
        self.assertFalse(payload["retryable"])
        self.assertEqual(payload["error_source"], "subagent")
        self.assertNotIn("timed out", payload["error"].lower())

    async def test_max_step_limit_response_is_treated_as_error(self) -> None:
        self.client._events = [
            {"event": "request.accepted", "data": {"request_id": "r1", "status": "accepted"}},
            {"event": "request.started", "data": {"request_id": "r1", "status": "started"}},
            {
                "event": "request.completed",
                "data": {
                    "request_id": "r1",
                    "status": "completed",
                    "response": {
                        "text": "Stopped after reaching the max step limit (30) before finishing.",
                        "metadata": {},
                    },
                },
            },
        ]

        result = await self.tool.execute({"agent_id": "research", "prompt": "hello"})

        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["error_source"], "subagent_response")
        self.assertEqual(payload["error_code"], "max_steps_exceeded")

    async def test_client_mode_invokes_resolved_client(self) -> None:
        client = _FakeClient()
        tool = InvokeSubagentTool(
            client_resolver=lambda _agent_id: client,
            primary_app_id="primary-app",
            primary_session_id_provider=lambda: "s2",
        )
        result = await tool.execute(
            {"agent_id": "research", "prompt": "Summarize issue", "opts": {"timeout_ms": 2500}}
        )
        self.assertFalse(result.is_error)
        expected_subagent_session_id = derive_subagent_session_id(
            "primary-app",
            "s2",
            "research",
        )
        payload = json.loads(result.content)
        self.assertEqual(payload["request_id"], "r1")
        self.assertEqual(payload["text"], "client-ok")
        self.assertEqual(payload["metadata"]["source"], "client")
        self.assertEqual(payload["subagent_session_id"], expected_subagent_session_id)
        self.assertEqual(result.metadata["subagent_session_id"], expected_subagent_session_id)
        assert client.last_call is not None
        self.assertEqual(client.last_call["session_id"], expected_subagent_session_id)
        self.assertEqual(client.last_call["timeout"], 2.5)
        self.assertEqual(
            client.last_call["primary_app_id"],  # type: ignore[index]
            "primary-app",
        )

    def test_constructor_requires_client_resolver(self) -> None:
        with self.assertRaises(ValueError):
            InvokeSubagentTool(
                client_resolver=None,  # type: ignore[arg-type]
                primary_app_id="primary-app",
                primary_session_id="s1",
            )


if __name__ == "__main__":
    unittest.main()

"""Tests for InvokeSubagent tool."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional

from mash.runtime.session import derive_subagent_session_id
from mash.tools.subagent import InvokeSubagentTool


class _FakeClient:
    def __init__(self) -> None:
        self.last_call: Optional[Dict[str, Any]] = None
        self._error: Optional[Exception] = None

    def invoke(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.last_call = {
            "message": message,
            "session_id": session_id,
            "turn_metadata": turn_metadata,
            "timeout_ms": timeout_ms,
        }
        if self._error:
            raise self._error
        return {
            "request_id": "r1",
            "status": "completed",
            "response": {"text": "client-ok", "metadata": {"source": "client"}},
        }


class InvokeSubagentToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeClient()
        self.tool = InvokeSubagentTool(
            client_resolver=lambda _agent_id: self.client,
            primary_app_id="primary-app",
            primary_session_id="s1",
        )

    def test_success_returns_json_payload(self) -> None:
        result = self.tool.execute(
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
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["subagent_session_id"], expected_subagent_session_id)
        self.assertEqual(payload["primary_app_id"], "primary-app")
        self.assertEqual(payload["request_id"], "r1")
        self.assertEqual(payload["text"], "client-ok")
        assert self.client.last_call is not None
        self.assertEqual(self.client.last_call["session_id"], expected_subagent_session_id)
        turn_metadata = self.client.last_call["turn_metadata"] or {}
        self.assertEqual(turn_metadata["primary_app_id"], "primary-app")
        self.assertEqual(turn_metadata["subagent_invoke_opts"]["x"], 1)

    def test_error_returns_error_result(self) -> None:
        self.client._error = TimeoutError("timed out")
        result = self.tool.execute({"agent_id": "research", "prompt": "hello"})
        self.assertTrue(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["agent_id"], "research")
        self.assertIn("timed out", payload["error"])

    def test_client_mode_invokes_resolved_client(self) -> None:
        client = _FakeClient()
        tool = InvokeSubagentTool(
            client_resolver=lambda _agent_id: client,
            primary_app_id="primary-app",
            primary_session_id_provider=lambda: "s2",
        )
        result = tool.execute(
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
        assert client.last_call is not None
        self.assertEqual(client.last_call["session_id"], expected_subagent_session_id)
        self.assertEqual(client.last_call["timeout_ms"], 2500)
        self.assertEqual(
            client.last_call["turn_metadata"]["primary_app_id"],  # type: ignore[index]
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

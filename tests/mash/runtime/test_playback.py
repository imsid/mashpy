"""Tests for shared runtime-event reasoning helpers."""

from __future__ import annotations

import unittest

from mash.runtime.events import RuntimeEvent
from mash.runtime.events import (
    build_reasoning_trace,
    runtime_event_to_trace_payload,
    runtime_trace_payload_response_preview,
    runtime_trace_payload_to_trace_payload,
)
from mash.runtime.events.types import RuntimeEventType


class PlaybackHelpersTests(unittest.TestCase):
    def test_runtime_trace_payload_maps_thinking_to_trace_payload(self) -> None:
        payload = runtime_trace_payload_to_trace_payload(
            {
                "event_type": RuntimeEventType.LLM_THINK_COMPLETED.value,
                "trace_id": "trace-1",
                "loop_index": 2,
                "payload": {
                    "action_type": "tool_call",
                    "assistant_text": "thinking",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                    "token_usage": {"input": 2, "output": 1},
                    "duration_ms": 123,
                },
            }
        )

        self.assertEqual(payload["event_type"], "agent.think.complete")
        self.assertEqual(payload["trace_id"], "trace-1")
        self.assertEqual(payload["step_id"], 2)
        self.assertEqual(payload["duration_ms"], 123)
        self.assertEqual(payload["action_type"], "tool_call")
        self.assertEqual(payload["tool_calls"], ["bash"])
        self.assertEqual(payload["token_usage"], {"input": 2, "output": 1})
        self.assertEqual(
            payload["payload"]["tool_calls_detail"][0]["name"],
            "bash",
        )

    def test_runtime_trace_payload_maps_tool_call_to_trace_payload(self) -> None:
        payload = runtime_trace_payload_to_trace_payload(
            {
                "event_type": RuntimeEventType.TOOL_CALL_COMPLETED.value,
                "trace_id": "trace-1",
                "loop_index": 1,
                "payload": {
                    "tool_name": "bash",
                    "duration_ms": 45,
                },
            }
        )

        self.assertEqual(payload["event_type"], "agent.act.complete")
        self.assertEqual(payload["action_type"], "tool_call")
        self.assertEqual(payload["tool_calls"], ["bash"])
        self.assertEqual(payload["duration_ms"], 45)

    def test_runtime_event_maps_subagent_call_to_trace_payload(self) -> None:
        payload = runtime_event_to_trace_payload(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
                trace_id="trace-1",
                loop_index=1,
                payload={"tool_name": "InvokeSubagent", "duration_ms": 67},
            )
        )

        self.assertEqual(payload["event_type"], "agent.act.complete")
        self.assertEqual(payload["action_type"], "subagent_call")
        self.assertEqual(payload["tool_calls"], ["InvokeSubagent"])
        self.assertEqual(payload["duration_ms"], 67)

    def test_runtime_trace_payload_maps_step_completed_to_trace_payload(self) -> None:
        payload = runtime_trace_payload_to_trace_payload(
            {
                "event_type": RuntimeEventType.STEP_COMPLETED.value,
                "trace_id": "trace-1",
                "loop_index": 0,
                "payload": {
                    "action_type": "response",
                    "tool_calls": [],
                    "duration_ms": 89,
                },
            }
        )

        self.assertEqual(payload["event_type"], "agent.step.complete")
        self.assertEqual(payload["action_type"], "response")
        self.assertEqual(payload["step_id"], 0)
        self.assertEqual(payload["duration_ms"], 89)

    def test_unsupported_event_returns_none(self) -> None:
        payload = runtime_trace_payload_to_trace_payload(
            {
                "event_type": RuntimeEventType.REQUEST_COMPLETED.value,
                "trace_id": "trace-1",
                "payload": {},
            }
        )

        self.assertIsNone(payload)

    def test_response_preview_only_returns_for_response_thinking(self) -> None:
        self.assertEqual(
            runtime_trace_payload_response_preview(
                {
                    "event_type": RuntimeEventType.LLM_THINK_COMPLETED.value,
                    "payload": {
                        "action_type": "response",
                        "assistant_text": "preview text",
                    },
                }
            ),
            "preview text",
        )
        self.assertEqual(
            runtime_trace_payload_response_preview(
                {
                    "event_type": RuntimeEventType.LLM_THINK_COMPLETED.value,
                    "payload": {
                        "action_type": "finish",
                        "assistant_text": "no preview",
                    },
                }
            ),
            "",
        )
        self.assertEqual(
            runtime_trace_payload_response_preview(
                {
                    "event_type": RuntimeEventType.TOOL_CALL_COMPLETED.value,
                    "payload": {
                        "assistant_text": "no preview",
                    },
                }
            ),
            "",
        )

    def test_trace_label_is_injected_into_thinking_payload(self) -> None:
        payload = runtime_trace_payload_to_trace_payload(
            {
                "event_type": RuntimeEventType.LLM_THINK_COMPLETED.value,
                "trace_id": "trace-sub-1",
                "loop_index": 0,
                "payload": {
                    "action_type": "tool_call",
                    "assistant_text": "checking cli flow",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                    "token_usage": {"input": 1, "output": 1},
                    "duration_ms": 7,
                },
            },
            trace_label="Subagent research",
        )

        self.assertEqual(
            payload["payload"]["trace_label"],
            "Subagent research",
        )

    def test_build_reasoning_trace_from_raw_events(self) -> None:
        trace = build_reasoning_trace(
            [
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    payload={
                        "action_type": "tool_call",
                        "assistant_text": "thinking",
                        "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                        "token_usage": {"input": 2, "output": 1},
                        "duration_ms": 123,
                    },
                ),
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    payload={"tool_name": "bash", "duration_ms": 45},
                ),
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.STEP_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    payload={
                        "action_type": "tool_call",
                        "tool_calls": ["bash"],
                        "duration_ms": 123,
                    },
                ),
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    payload={},
                ),
            ]
        )

        self.assertEqual(trace["status"], "completed")
        self.assertEqual(trace["summary"]["total_steps"], 1)
        self.assertEqual(trace["steps"][0]["title"], "Calling tools: bash")
        self.assertEqual(trace["steps"][0]["tool_calls"][0]["preview"], "$ pwd")


if __name__ == "__main__":
    unittest.main()

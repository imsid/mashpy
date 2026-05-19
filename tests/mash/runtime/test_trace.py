"""Tests for shared runtime trace parsing helpers."""

from __future__ import annotations

import unittest

from mash.runtime.events import (
    RuntimeEvent,
    build_reasoning_trace,
    build_runtime_trace,
    runtime_event_from_stream_payload,
    runtime_event_response_preview,
)
from mash.runtime.events.types import RuntimeEventType


class RuntimeTraceTests(unittest.TestCase):
    def test_response_preview_only_returns_for_response_thinking(self) -> None:
        response_event = RuntimeEvent(
            app_id="primary",
            agent_id="primary",
            event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
            payload={
                "action_type": "response",
                "assistant_text": "preview text",
            },
        )
        finish_event = RuntimeEvent(
            app_id="primary",
            agent_id="primary",
            event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
            payload={
                "action_type": "finish",
                "assistant_text": "no preview",
            },
        )
        tool_event = RuntimeEvent(
            app_id="primary",
            agent_id="primary",
            event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
            payload={"assistant_text": "no preview"},
        )

        self.assertEqual(runtime_event_response_preview(response_event), "preview text")
        self.assertEqual(runtime_event_response_preview(finish_event), "")
        self.assertEqual(runtime_event_response_preview(tool_event), "")

    def test_stream_payload_hydrates_runtime_event(self) -> None:
        event = runtime_event_from_stream_payload(
            {
                "event_type": RuntimeEventType.LLM_THINK_COMPLETED.value,
                "trace_id": "trace-1",
                "session_id": "s-1",
                "loop_index": 2,
                "created_at": 100.0,
                "payload": {
                    "action_type": "tool_call",
                    "assistant_text": "thinking",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                    "token_usage": {"input": 2, "output": 1},
                    "duration_ms": 123,
                },
            },
            app_id="primary",
        )

        assert event is not None
        self.assertEqual(event.event_type, RuntimeEventType.LLM_THINK_COMPLETED.value)
        self.assertEqual(event.app_id, "primary")
        self.assertEqual(event.agent_id, "primary")
        self.assertEqual(event.trace_id, "trace-1")
        self.assertEqual(event.session_id, "s-1")
        self.assertEqual(event.loop_index, 2)
        self.assertEqual(event.created_at, 100.0)
        self.assertEqual(event.payload["duration_ms"], 123)

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

    def test_build_runtime_trace_extracts_runtime_fields(self) -> None:
        trace = build_runtime_trace(
            [
                RuntimeEvent(
                    event_id=1,
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.TRACE_STARTED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=1.0,
                    payload={"message": "runtime user"},
                ),
                RuntimeEvent(
                    event_id=2,
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    created_at=2.0,
                    payload={
                        "action_type": "tool_call",
                        "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                        "token_usage": {"input": 8, "output": 3},
                    },
                ),
                RuntimeEvent(
                    event_id=3,
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    created_at=3.0,
                    payload={"tool_name": "bash"},
                ),
                RuntimeEvent(
                    event_id=4,
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.STEP_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    loop_index=0,
                    created_at=4.0,
                    payload={},
                ),
                RuntimeEvent(
                    event_id=5,
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.REQUEST_COMPLETED.value,
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=5.0,
                    payload={"response": {"text": "runtime answer"}},
                ),
            ]
        )

        self.assertEqual(trace.target_agent_id, "primary")
        self.assertEqual(trace.session_id, "s-1")
        self.assertEqual(trace.trace_id, "trace-1")
        self.assertEqual(trace.status, "completed")
        self.assertEqual(trace.user_message, "runtime user")
        self.assertEqual(trace.assistant_response, "runtime answer")
        self.assertEqual(trace.tools_called, ["bash"])
        self.assertEqual(trace.tool_call_count, 1)
        self.assertEqual(trace.step_count, 1)
        self.assertEqual(trace.input_tokens, 8)
        self.assertEqual(trace.output_tokens, 3)
        self.assertEqual(trace.duration_ms, 4000.0)

    def test_build_runtime_trace_keeps_legacy_fallbacks(self) -> None:
        trace = build_runtime_trace(
            [
                RuntimeEvent(
                    event_id=1,
                    app_id="primary",
                    agent_id="primary",
                    event_type="agent.run.start",
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=1.0,
                    payload={"user_message": "legacy user"},
                ),
                RuntimeEvent(
                    event_id=2,
                    app_id="primary",
                    agent_id="primary",
                    event_type="agent.tool.error",
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=2.0,
                    payload={"tool_name": "search", "status": "failed"},
                ),
                RuntimeEvent(
                    event_id=3,
                    app_id="primary",
                    agent_id="primary",
                    event_type="llm.request.complete",
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=3.0,
                    payload={"input_tokens": 5, "output_tokens": 2},
                ),
                RuntimeEvent(
                    event_id=4,
                    app_id="primary",
                    agent_id="primary",
                    event_type="agent.run.complete",
                    trace_id="trace-1",
                    session_id="s-1",
                    created_at=4.0,
                    payload={"assistant_response": "legacy answer"},
                ),
            ]
        )

        self.assertEqual(trace.user_message, "legacy user")
        self.assertEqual(trace.assistant_response, "legacy answer")
        self.assertEqual(trace.tools_called, ["search"])
        self.assertEqual(trace.tool_call_count, 1)
        self.assertEqual(trace.tool_error_count, 1)
        self.assertEqual(len(trace.failed_events), 1)
        self.assertEqual(trace.input_tokens, 5)
        self.assertEqual(trace.output_tokens, 2)


if __name__ == "__main__":
    unittest.main()


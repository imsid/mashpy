"""llm.response.delta surfaces through the event spine to the CLI hydrator."""

from __future__ import annotations

import unittest

from mash.logging.events import LLMEvent
from mash.logging.logger import EventLogger
from mash.runtime.events.trace import runtime_event_from_stream_payload
from mash.runtime.requests import to_public_event


class DeltaEventSpineTests(unittest.TestCase):
    def test_delta_event_round_trips_to_cli_hydrator(self) -> None:
        # 1. Provider-emitted delta event (as BaseLLMProvider._emit_response_delta builds it).
        event = LLMEvent(
            event_type="llm.response.delta",
            app_id="pilot",
            session_id="s1",
            provider="anthropic",
            model="claude-sonnet-4-6",
            trace_id="trace-123",
            payload={"text": "hello world", "index": 3},
        )

        # 2. EventLogger normalizes it into a RuntimeEvent (the persisted shape).
        runtime_event = EventLogger._to_runtime_event(event)
        self.assertEqual(runtime_event.event_type, "llm.response.delta")
        self.assertEqual(runtime_event.trace_id, "trace-123")

        # 3. SSE mapping: unknown types fall through to the generic agent.trace frame.
        public = to_public_event(runtime_event)
        self.assertEqual(public["event"], "agent.trace")
        data = public["data"]
        self.assertEqual(data["event_type"], "llm.response.delta")

        # 4. CLI hydration produces a RuntimeEvent whose payload carries the
        #    LLMEvent's wrapped {"payload": {text, index}} chunk.
        hydrated = runtime_event_from_stream_payload(data, app_id="pilot")
        self.assertIsNotNone(hydrated)
        assert hydrated is not None
        self.assertEqual(hydrated.event_type, "llm.response.delta")
        inner = hydrated.payload.get("payload")
        self.assertIsInstance(inner, dict)
        self.assertEqual(inner.get("text"), "hello world")
        self.assertEqual(inner.get("index"), 3)


if __name__ == "__main__":
    unittest.main()

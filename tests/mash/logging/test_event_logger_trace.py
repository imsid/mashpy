"""How an emitted event acquires its trace id.

An LLM provider carries the trace id as an instance field, and the runtime
shares one provider instance across every request an agent serves. Two
requests running at once therefore overwrite each other's value, so the
ambient trace context is the authority when it is set.
"""

from __future__ import annotations

import asyncio
import unittest

from mash.logging.events import LLMEvent
from mash.logging.logger import EventLogger
from mash.logging.trace_context import (
    set_request_id,
    set_session_id,
    set_trace_id,
)


class _CollectingStore:
    def __init__(self) -> None:
        self.events: list = []

    async def append_event(self, event):  # noqa: ANN001 - test double
        self.events.append(event)
        return event


def _llm_event(*, trace_id: str | None, session_id: str = "session-1") -> LLMEvent:
    return LLMEvent(
        event_type="llm.request.complete",
        app_id="agent-1",
        session_id=session_id,
        provider="test",
        model="test-model",
        trace_id=trace_id,
    )


class EventLoggerTraceResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambient_trace_wins_over_a_stale_event_value(self) -> None:
        store = _CollectingStore()
        logger = EventLogger(store)

        set_trace_id("trace-mine")
        set_request_id("req-mine")
        # The shared provider still holds the trace of whichever request
        # configured it last.
        await logger.emit(_llm_event(trace_id="trace-someone-else"))

        self.assertEqual(store.events[-1].trace_id, "trace-mine")
        self.assertEqual(store.events[-1].request_id, "req-mine")

    async def test_event_value_used_when_no_ambient_trace(self) -> None:
        store = _CollectingStore()
        logger = EventLogger(store)

        set_trace_id(None)
        set_request_id(None)
        await logger.emit(_llm_event(trace_id="trace-explicit"))

        self.assertEqual(store.events[-1].trace_id, "trace-explicit")

    async def test_concurrent_requests_keep_their_own_trace(self) -> None:
        """Two requests sharing one provider instance must not cross-stamp.

        Each task sets its own trace context, then emits with the *other*
        request's trace id on the event, standing in for a provider whose
        instance field was overwritten mid-flight.
        """
        store = _CollectingStore()
        logger = EventLogger(store)

        async def run_request(name: str, stale: str) -> None:
            set_trace_id(f"trace-{name}")
            set_request_id(f"req-{name}")
            set_session_id("session-1")
            await asyncio.sleep(0)  # let the other task interleave
            await logger.emit(_llm_event(trace_id=stale))

        await asyncio.gather(
            run_request("a", stale="trace-b"),
            run_request("b", stale="trace-a"),
        )

        by_request = {e.request_id: e.trace_id for e in store.events}
        self.assertEqual(by_request["req-a"], "trace-a")
        self.assertEqual(by_request["req-b"], "trace-b")

    async def test_concurrent_sessions_keep_their_own_session(self) -> None:
        """The session is instance state on the provider too.

        Two requests in different sessions would otherwise stamp each other's
        events, which skews session history and every session rollup.
        """
        store = _CollectingStore()
        logger = EventLogger(store)

        async def run_request(name: str, stale_session: str) -> None:
            set_trace_id(f"trace-{name}")
            set_request_id(f"req-{name}")
            set_session_id(f"session-{name}")
            await asyncio.sleep(0)
            await logger.emit(
                _llm_event(trace_id=f"trace-{name}", session_id=stale_session)
            )

        await asyncio.gather(
            run_request("a", stale_session="session-b"),
            run_request("b", stale_session="session-a"),
        )

        by_request = {e.request_id: e.session_id for e in store.events}
        self.assertEqual(by_request["req-a"], "session-a")
        self.assertEqual(by_request["req-b"], "session-b")

    async def test_event_session_used_when_no_ambient_session(self) -> None:
        store = _CollectingStore()
        logger = EventLogger(store)

        set_trace_id(None)
        set_request_id(None)
        set_session_id(None)
        await logger.emit(_llm_event(trace_id="trace-explicit"))

        self.assertEqual(store.events[-1].session_id, "session-1")


class AgentSessionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        # These bind context vars in the caller's context rather than a task
        # copy, so restore them; a leaked session reaches later tests.
        from mash.logging.trace_context import get_session_id, get_trace_id

        previous_session, previous_trace = get_session_id(), get_trace_id()
        self.addCleanup(lambda: set_session_id(previous_session))
        self.addCleanup(lambda: set_trace_id(previous_trace))

    def test_set_event_logger_binds_the_ambient_session(self) -> None:
        """The hosted runtime never calls run(), so binding happens here.

        configure_turn_context calls set_event_logger for each turn; without
        the ambient bind, a durable request would have no session in context
        and the logger would fall back to the provider's instance field.
        """
        from mash.core.agent import Agent
        from mash.core.config import AgentConfig
        from mash.logging.trace_context import get_session_id
        from mash.skills.registry import SkillRegistry
        from mash.testing.runtime_fixtures import DeterministicLLMProvider
        from mash.tools.registry import ToolRegistry

        set_session_id(None)
        agent = Agent(
            llm=DeterministicLLMProvider(response_text="ok"),
            tools=ToolRegistry(),
            skills=SkillRegistry(),
            config=AgentConfig(app_id="binding-app", system_prompt="test"),
        )
        agent.set_event_logger(EventLogger(_CollectingStore()), "session-bound")

        self.assertEqual(get_session_id(), "session-bound")


if __name__ == "__main__":
    unittest.main()

"""The board: what a request the companion routed sees at context load.

- every entry since the latest summary, in full, labelled with its number and
  author, whichever agent wrote it; no turn window
- the context-load event records the board position, and recovery rebuilds
  from it, so entries added later do not change a recovered request
- the request's own entry goes on the board at commit, sized by its context
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import patch

from mash.runtime.engine import loop as loop_module
from mash.runtime.engine.rebuild import rebuild_request_state
from mash.runtime.events import RuntimeEventType
from mash.testing.runtime_fixtures import TEST_IDENTITY

from test_replay import ReplayTestCase, _Provider, _Spec

SESSION = "conv-1"


def _parent(request_id: str = "companion-req") -> dict[str, Any]:
    return {
        "kind": "conversation",
        "request_id": request_id,
        "agent_id": "companion",
        "session_id": SESSION,
        "started_by": "companion",
        "parent_request_id": request_id,
    }


def _texts(request: Any) -> list[tuple[str, str]]:
    return [(m.role, m.content[0].data.get("text")) for m in request.messages]


class _BoardCase(ReplayTestCase):
    async def _entry(self, name: str, agent_id: str, **metadata: Any) -> int:
        return await self.memory_store.append_entry(
            trace_id=name,
            session_id=SESSION,
            agent_id=agent_id,
            user_message=f"asked {name}",
            agent_response=f"{agent_id} answered {name}" if metadata.get("type") != "failed_request" else "",
            signals={},
            context_tokens=5,
            tokens=5,
            metadata=metadata or None,
        )

    async def _routed(self, runtime: Any, message: str) -> Any:
        accepted = await runtime.submit_request(
            message=message, session_id=SESSION, parent=_parent(), **TEST_IDENTITY
        )
        for _ in range(500):
            events = await self.runtime_store.list_request_events(accepted["request_id"])
            ends = [e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_END.value]
            if ends:
                self.assertEqual(ends[0].payload["status"], "ok", ends[0].payload)
                return ends[0]
            await asyncio.sleep(0.01)
        self.fail("the request never ended")


class BoardContextTests(_BoardCase):
    async def test_a_routed_request_reads_every_agents_entries_labelled(self) -> None:
        await self._entry("e1", "inbox")
        await self._entry("e2", "fudge")
        await self._entry("e3", "inbox", type="failed_request", error={"error": "boom"})
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider, conversation_history_turns=1))

        await self._routed(runtime, "next")

        texts = _texts(provider.requests[0])
        self.assertEqual(
            texts[:4],
            [
                ("user", "[entry 1 · inbox]\nasked e1"),
                ("assistant", "inbox answered e1"),
                ("user", "[entry 2 · fudge]\nasked e2"),
                ("assistant", "fudge answered e2"),
            ],
        )
        # The failed entry and its error go before the new message.
        role, last = texts[4]
        self.assertEqual(role, "user")
        self.assertTrue(last.startswith("[entry 3 · inbox · failed]"), last)
        self.assertIn("boom", last)
        self.assertTrue(last.endswith("next"), last)

    async def test_the_board_starts_at_the_latest_summary(self) -> None:
        await self._entry("e1", "inbox")
        await self._entry("s2", "companion", type="summary")
        await self._entry("e3", "fudge")
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))

        await self._routed(runtime, "next")

        self.assertEqual(
            _texts(provider.requests[0]),
            [
                ("user", "[entry 2 · companion · summary of the entries before it]"),
                ("assistant", "companion answered s2"),
                ("user", "[entry 3 · fudge]\nasked e3"),
                ("assistant", "fudge answered e3"),
                ("user", "next"),
            ],
        )

    async def test_the_routed_reply_is_the_next_entry(self) -> None:
        await self._entry("e1", "inbox")
        provider = _Provider(context=40)
        runtime = await self._runtime(_Spec(provider))

        await self._routed(runtime, "next")

        entry = await self.memory_store.last_entry(SESSION)
        self.assertEqual((entry["seq"], entry["agent_id"]), (2, "replay-app"))
        self.assertEqual((entry["user_message"], entry["agent_response"]), ("next", "answered"))
        self.assertEqual((entry["tokens"], entry["board_tokens"]), (40, 45))

    async def test_the_context_load_records_the_position_and_recovery_rebuilds_it(self) -> None:
        await self._entry("e1", "inbox")
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))
        loaded: list[dict[str, Any]] = []
        load = loop_module.load_request_context

        async def _load(*args: Any, **kwargs: Any) -> dict[str, Any]:
            state = await load(*args, **kwargs)
            loaded.append(copy.deepcopy(state))
            return state

        with patch.object(loop_module, "load_request_context", _load):
            end = await self._routed(runtime, "next")
        # Entries added after the load do not change what a recovery reads.
        await self._entry("later", "fudge")

        events = await self.runtime_store.list_request_events(end.request_id)
        load_end = next(
            e for e in events if e.event_type == RuntimeEventType.CONTEXT_LOAD_END.value
        )
        self.assertEqual(load_end.payload["board"], {"up_to": 1})
        self.assertNotIn("history", load_end.payload)
        start = next(e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value)
        rebuilt = await rebuild_request_state(
            runtime,
            session_id=SESSION,
            message="next",
            request_metadata=dict(start.payload["request_metadata"]),
            events=[e for e in events if e.event_id <= load_end.event_id],
        )
        self.assertEqual(rebuilt, loaded[0])

    async def test_an_empty_board_records_position_zero(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))

        end = await self._routed(runtime, "first")

        events = await self.runtime_store.list_request_events(end.request_id)
        load_end = next(
            e for e in events if e.event_type == RuntimeEventType.CONTEXT_LOAD_END.value
        )
        self.assertEqual(load_end.payload["board"], {"up_to": 0})
        self.assertEqual(_texts(provider.requests[0]), [("user", "first")])


class _CompanionSpec(_Spec):
    def get_agent_id(self) -> str:
        return "companion"

    def build_agent_config(self):  # type: ignore[override]
        from mash.core.config import AgentConfig

        return AgentConfig(agent_id="companion", system_prompt="route", **self.config)


class BoardCompactionInRequestsTests(_BoardCase):
    async def test_a_routed_request_never_compacts_on_its_own(self) -> None:
        await self._entry("e1", "inbox")
        provider = _Provider(usage=600)
        runtime = await self._runtime(_Spec(provider, compaction_token_threshold=100))

        await self._routed(runtime, "next")
        await self._routed(runtime, "again")

        self.assertEqual(provider.summaries, [])
        entries = await self.memory_store.read_entries(SESSION)
        self.assertEqual([e["metadata"].get("type") for e in entries], [None, None, None])

    async def test_compacting_the_companions_session_summarizes_the_board(self) -> None:
        await self._entry("e1", "inbox")
        await self._entry("e2", "fudge")
        provider = _Provider()
        runtime = await self._runtime(_CompanionSpec(provider))

        text, trace_id = await runtime.compact_session(SESSION, reason="manual")

        self.assertEqual(text, "the summary")
        entry = await self.memory_store.last_entry(SESSION)
        self.assertEqual(
            (entry["seq"], entry["agent_id"], entry["trace_id"], entry["metadata"]["type"]),
            (3, "companion", trace_id, "summary"),
        )
        self.assertIn("[entry 2 · fudge]", provider.summaries[0])


class BoardToolsInRequestsTests(_BoardCase):
    async def test_only_a_routed_request_gets_the_board_tools(self) -> None:
        provider = _Provider()
        runtime = await self._runtime(_Spec(provider))

        await self._routed(runtime, "routed")
        await self._ask(runtime, "own", session_id="own-session")

        routed, own = provider.requests[0], provider.requests[2]
        self.assertTrue({"read_board", "search_board"} <= {t.name for t in routed.tools})
        self.assertFalse({"read_board", "search_board"} & {t.name for t in own.tools})

"""EventStore against a real Postgres: dedup, one active event, order, bindings, deletes."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from mash.policies import Policy, PolicyNotFoundError, PolicyStore
from mash.sessions import EventStore, PolicyInUseError, SessionMismatchError, event_request_id

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


def _database_url() -> str:
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


def _defined(host_id: str) -> bool:
    return host_id != "undefined"


class EventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.url = _database_url()
        self.tag = uuid.uuid4().hex[:12]
        self.policy_id = f"p-{self.tag}"
        self.policies = PolicyStore(self.url)
        self.store = EventStore(self.url, self.policies)
        await self.policies.put(self.policy_id, Policy(sandbox=False))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.policies.close()
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DELETE FROM events WHERE session_id LIKE %s", (f"%{self.tag}%",))
            conn.execute("DELETE FROM sessions WHERE session_id LIKE %s", (f"%{self.tag}%",))
            conn.execute("DELETE FROM policies WHERE policy_id LIKE %s", (f"%{self.tag}%",))

    def _session(self, name: str = "s") -> str:
        return f"{name}-{self.tag}"

    async def _insert(self, session_id: str, event_id: str, **kw):
        return await self.store.insert_event(
            session_id,
            event_id,
            host_id=kw.get("host_id", "assistant"),
            policy_id=kw.get("policy_id", self.policy_id),
            body={"app_id": "app-1", "message": event_id},
        )

    async def test_concurrent_posts_of_one_event_make_one_row(self) -> None:
        session = self._session()
        results = await asyncio.gather(*(self._insert(session, "e-1") for _ in range(8)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({record.seq for record, _ in results}, {results[0][0].seq})
        rows = await self.store.list_events(session_id=session)
        self.assertEqual(len(rows), 1)

    async def test_at_most_one_event_starts_per_session_and_in_seq_order(self) -> None:
        session = self._session()
        for event_id in ("e-1", "e-2", "e-3"):
            await self._insert(session, event_id)
        claims = await asyncio.gather(
            *(self.store.claim_next(session, _defined) for _ in range(6))
        )
        started = [claim for claim in claims if claim is not None]
        self.assertEqual(len(started), 1)
        record, binding, policy = started[0]
        self.assertEqual(record.event_id, "e-1")
        self.assertEqual(record.request_id, event_request_id(session, "e-1"))
        self.assertEqual((binding.host_id, binding.policy_id), ("assistant", self.policy_id))
        self.assertFalse(policy["sandbox"])

        self.assertTrue(
            await self.store.close_event(session, "e-1", status="completed", response={"text": "x"}, error=None)
        )
        # A repeated close changes nothing.
        self.assertFalse(
            await self.store.close_event(session, "e-1", status="failed", response=None, error={"error": "late"})
        )
        closed = await self.store.get_event(session, "e-1")
        self.assertEqual((closed.status, closed.response), ("completed", {"text": "x"}))
        self.assertIsNotNone(closed.closed_at)

        next_record, _, _ = await self.store.claim_next(session, _defined)
        self.assertEqual(next_record.event_id, "e-2")

    async def test_the_one_active_index_refuses_a_second_started_event(self) -> None:
        session = self._session()
        await self._insert(session, "e-1")
        await self._insert(session, "e-2")
        await self.store.claim_next(session, _defined)
        with psycopg.connect(self.url, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "UPDATE events SET status = 'started' WHERE session_id = %s AND event_id = 'e-2'",
                    (session,),
                )

    async def test_an_undefined_host_leaves_the_event_queued(self) -> None:
        session = self._session()
        await self._insert(session, "e-1", host_id="undefined")
        self.assertIsNone(await self.store.claim_next(session, _defined))
        self.assertEqual((await self.store.get_event(session, "e-1")).status, "queued")
        self.assertEqual(await self.store.sessions_with_queued("undefined"), [session])

    async def test_a_session_keeps_its_host_and_policy(self) -> None:
        session = self._session()
        other_policy = f"p2-{self.tag}"
        await self.policies.put(other_policy, Policy())
        await self._insert(session, "e-1")
        with self.assertRaises(SessionMismatchError) as host:
            await self._insert(session, "e-2", host_id="other")
        self.assertEqual(host.exception.kind, "host")
        with self.assertRaises(SessionMismatchError) as policy:
            await self._insert(session, "e-3", policy_id=other_policy)
        self.assertEqual(policy.exception.kind, "policy")
        with self.assertRaises(PolicyNotFoundError):
            await self._insert(self._session("new"), "e-1", policy_id=f"missing-{self.tag}")
        self.assertEqual(len(await self.store.list_events(session_id=session)), 1)

    async def test_cancel_takes_only_a_queued_event(self) -> None:
        session = self._session()
        await self._insert(session, "e-1")
        await self._insert(session, "e-2")
        await self.store.claim_next(session, _defined)
        self.assertIsNone(await self.store.cancel_queued(session, "e-1"))
        cancelled = await self.store.cancel_queued(session, "e-2")
        self.assertEqual((cancelled.status, cancelled.request_id), ("cancelled", None))

    async def test_a_policy_with_queued_or_started_events_cannot_be_deleted(self) -> None:
        session = self._session()
        await self._insert(session, "e-1")
        removed: list[str] = []

        async def remove() -> None:
            removed.append(self.policy_id)

        with self.assertRaises(PolicyInUseError):
            await self.store.delete_policy(self.policy_id, remove)
        await self.store.claim_next(session, _defined)
        with self.assertRaises(PolicyInUseError):
            await self.store.delete_policy(self.policy_id, remove)
        await self.store.close_event(session, "e-1", status="completed", response=None, error=None)
        await self.store.delete_policy(self.policy_id, remove)
        self.assertEqual(removed, [self.policy_id])
        self.assertIsNone(await self.policies.get(self.policy_id))
        # The session and its ledger stay; a new event finds no policy.
        self.assertEqual(len(await self.store.list_events(session_id=session)), 1)
        with self.assertRaises(PolicyNotFoundError):
            await self._insert(session, "e-2")
        with self.assertRaises(PolicyNotFoundError):
            await self.store.delete_policy(self.policy_id, remove)

    async def test_the_host_listing_covers_its_sessions_in_seq_order(self) -> None:
        one, two = self._session("a"), self._session("b")
        await self._insert(one, "e-1")
        await self._insert(two, "e-1", host_id=f"h-{self.tag}")
        await self._insert(one, "e-2")
        rows = await self.store.list_events(host_id="assistant", statuses=["queued"])
        mine = [(r.session_id, r.event_id) for r in rows if r.session_id in (one, two)]
        self.assertEqual(mine, [(one, "e-1"), (one, "e-2")])
        first = mine and [r for r in rows if r.session_id == one][0]
        later = await self.store.list_events(host_id="assistant", after_seq=first.seq)
        self.assertNotIn(first.seq, [r.seq for r in later])

"""runtime_request's rollup columns and the listings built on them (real Postgres)."""

from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path
from typing import Any

from conftest import seed_lifecycle
from mash.runtime.events.store import PostgresRuntimeStore
from mash.runtime.events.types import RuntimeEvent, RuntimeEventType

try:  # pragma: no cover - environment dependent
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None

ACCEPTED = RuntimeEventType.INVOKE_AGENT_START.value
END = RuntimeEventType.INVOKE_AGENT_END.value
CHAT_END = RuntimeEventType.CHAT_END.value
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "src/mash/storage/migrations/016_request_rollup.sql"
)


def _database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )


def _usage(input_tokens: int, output_tokens: int, read: int = 0, write: int = 0) -> dict[str, Any]:
    return {
        "attributes": {
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "gen_ai.usage.cache_read.input_tokens": read,
            "gen_ai.usage.cache_creation.input_tokens": write,
        }
    }


class RequestRollupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        self.url = _database_url()
        try:
            with psycopg.connect(self.url, autocommit=True) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on local postgres
            raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
        self.tag = f"rollup-{uuid.uuid4().hex[:10]}"
        self.store = PostgresRuntimeStore(self.url)
        await self.store.open()
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self) -> None:
        await self.store.close()
        like = f"{self.tag}%"
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DELETE FROM runtime_event_log WHERE request_id LIKE %s", (like,))
            conn.execute("DELETE FROM runtime_request WHERE request_id LIKE %s", (like,))
            conn.execute("DELETE FROM runtime_request_seq WHERE request_id LIKE %s", (like,))

    def _agent(self, name: str) -> str:
        return f"{self.tag}-{name}"

    async def _event(
        self,
        request: str,
        event_type: str,
        *,
        agent: str = "a",
        session: str = "s1",
        created_at: float,
        dedupe: str | None = None,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
        host_id: str | None = None,
        workflow_id: str | None = None,
    ) -> RuntimeEvent:
        request_id = f"{self.tag}-{request}"
        return await self.store.append_event(
            RuntimeEvent(
                app_id=self._agent(agent),
                agent_id=self._agent(agent),
                request_id=request_id,
                session_id=f"{self.tag}-{session}",
                trace_id=f"trace-{request_id}",
                host_id=host_id,
                workflow_id=workflow_id,
                event_type=event_type,
                lifecycle=seed_lifecycle(event_type, status),
                dedupe_key=dedupe,
                created_at=created_at,
                payload=payload or ({"status": status} if status else {}),
            )
        )

    async def _request(
        self,
        request: str,
        *,
        agent: str = "a",
        session: str = "s1",
        at: float,
        tokens: tuple[int, int] = (0, 0),
        done: bool = True,
        host_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        common = {"agent": agent, "session": session}
        await self._event(request, ACCEPTED, created_at=at, dedupe="request.accepted", **common)
        await self._event(
            request,
            CHAT_END,
            created_at=at + 1,
            dedupe="chat.end.0",
            payload=_usage(*tokens),
            host_id=host_id,
            workflow_id=workflow_id,
            **common,
        )
        if done:
            await self._event(
                request, END, created_at=at + 2, dedupe="request.completed", status="ok", **common
            )

    def _row(self, request: str) -> dict[str, Any]:
        with psycopg.connect(self.url, autocommit=True, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT * FROM runtime_request WHERE request_id = %s",
                (f"{self.tag}-{request}",),
            ).fetchone()

    async def test_chat_end_adds_tokens_once_and_events_fill_the_rollup(self) -> None:
        await self._event("r1", ACCEPTED, created_at=100.0, dedupe="request.accepted")
        first = await self._event(
            "r1", CHAT_END, created_at=101.0, dedupe="chat.end.0",
            payload=_usage(10, 5, 3, 2), host_id="assistant", workflow_id="wf",
        )
        await self._event(
            "r1", CHAT_END, created_at=101.0, dedupe="chat.end.0",
            payload=_usage(10, 5, 3, 2),
        )
        await self._event(
            "r1", CHAT_END, created_at=102.0, dedupe="chat.end.1", payload=_usage(1, 1)
        )

        row = self._row("r1")
        self.assertEqual(
            (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]),
            (11, 6, 3, 2),
        )
        self.assertEqual(row["trace_id"], first.trace_id)
        self.assertEqual(row["host_id"], "assistant")
        self.assertEqual(row["workflow_id"], "wf")
        self.assertEqual(row["last_event_at"], 102.0)

    async def test_traces_page_comes_from_requests_newest_first(self) -> None:
        await self._request("old", at=100.0, tokens=(1, 1))
        await self._request("mid", agent="b", at=200.0, tokens=(2, 2))
        await self._request("new", at=300.0, tokens=(3, 3), done=False)
        await self._request("elsewhere", session="s2", at=400.0)

        session = f"{self.tag}-s1"
        traces = await self.store.list_recent_traces(session_id=session, limit=10)
        self.assertEqual(
            [t["trace_id"] for t in traces],
            [f"trace-{self.tag}-{name}" for name in ("new", "mid", "old")],
        )
        newest = traces[0]
        self.assertEqual(newest["status"], "in_progress")
        self.assertEqual(newest["event_count"], 2)
        self.assertEqual(newest["total_tokens"], 6)
        self.assertEqual(newest["started_at"], 300.0)
        self.assertEqual(newest["latest_event_at"], 301.0)
        self.assertEqual(traces[1]["agent_id"], self._agent("b"))
        self.assertEqual(traces[2]["status"], "completed")

        only_a = await self.store.list_recent_traces(self._agent("a"), session_id=session)
        self.assertEqual(len(only_a), 2)
        self.assertEqual(
            len(await self.store.list_recent_traces(session_id=session, limit=1)), 1
        )
        self.assertEqual(
            [t["trace_id"] for t in await self.store.list_recent_traces(
                session_id=session, status="in_progress")],
            [f"trace-{self.tag}-new"],
        )

    async def test_sessions_roll_up_their_requests(self) -> None:
        await self._request("p1", agent="primary", at=100.0, tokens=(10, 10))
        await self._request("c1", agent="child", at=110.0, tokens=(5, 5), host_id="assistant")
        await self._request(
            "w1", agent="primary", session="s2", at=500.0, tokens=(1, 0), workflow_id="wf-x"
        )

        result = await self.store.list_sessions(agent_id=self._agent("child"))
        [s1] = result["sessions"]
        self.assertEqual(result["total"], 1)
        self.assertEqual(s1["session_id"], f"{self.tag}-s1")
        self.assertEqual(s1["owner_agent_id"], self._agent("primary"))
        self.assertEqual(sorted(s1["agent_ids"]), sorted([self._agent("primary"), self._agent("child")]))
        self.assertEqual(s1["host_id"], "assistant")
        self.assertEqual(s1["trace_count"], 2)
        self.assertEqual(s1["total_tokens"], 30)
        self.assertEqual(s1["latest_event_at"], 112.0)

        primary = await self.store.list_sessions(agent_id=self._agent("primary"))
        self.assertEqual(
            [s["session_id"] for s in primary["sessions"]],
            [f"{self.tag}-s2", f"{self.tag}-s1"],
        )
        by_workflow = await self.store.list_sessions(workflow_id="wf-x")
        self.assertIn(f"{self.tag}-s2", [s["session_id"] for s in by_workflow["sessions"]])
        self.assertNotIn(f"{self.tag}-s1", [s["session_id"] for s in by_workflow["sessions"]])

        self.assertEqual(
            await self.store.session_usage(f"{self.tag}-s1"),
            {"total_tokens": 30, "cache_read_tokens": 0, "cache_write_tokens": 0},
        )
        self.assertEqual(
            await self.store.session_usage(f"{self.tag}-none"),
            {"total_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
        )

    async def test_migration_backfills_the_rollup_from_the_log(self) -> None:
        await self._request("legacy", at=100.0, tokens=(7, 3), host_id="assistant")
        request_id = f"{self.tag}-legacy"
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(
                "UPDATE runtime_request SET trace_id = NULL, host_id = NULL, "
                "last_event_at = NULL, input_tokens = 0, output_tokens = 0 "
                "WHERE request_id = %s",
                (request_id,),
            )
        sql = MIGRATION.read_text()
        start = sql.index("UPDATE runtime_request AS r")
        end = sql.index("WHERE rollup.request_id = r.request_id;")
        backfill = (
            sql[start:end]
            + "WHERE rollup.request_id = r.request_id AND r.request_id = %s"
        )
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(backfill, (request_id,))

        row = self._row("legacy")
        self.assertEqual(row["trace_id"], f"trace-{request_id}")
        self.assertEqual(row["host_id"], "assistant")
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (7, 3))
        self.assertEqual(row["last_event_at"], 102.0)

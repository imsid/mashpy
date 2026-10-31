"""Request mode columns on runtime_request, against a real Postgres."""

from __future__ import annotations

import os
import unittest
import uuid
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


def _database_url() -> str:
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/mash",
    )
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


def _request_row(database_url: str, request_id: str) -> dict[str, Any]:
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT status, session_id, mode, started_by, started_at,
                   parent_request_id, updated_at
            FROM runtime_request WHERE request_id = %s
            """,
            (request_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


class RequestModeStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = _database_url()
        self.session_id = f"mode-session-{uuid.uuid4()}"
        self.request_ids: list[str] = []
        self.store = PostgresRuntimeStore(self.database_url)
        await self.store.open()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM runtime_event_log WHERE request_id = ANY(%s)",
                (self.request_ids,),
            )
            conn.execute(
                "DELETE FROM runtime_request WHERE request_id = ANY(%s)",
                (self.request_ids,),
            )

    async def _append(
        self,
        request_id: str,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        created_at: float,
        dedupe_key: str | None = None,
        agent_id: str = "mode-agent",
    ) -> RuntimeEvent:
        if request_id not in self.request_ids:
            self.request_ids.append(request_id)
        return await self.store.append_event(
            RuntimeEvent(
                app_id=agent_id,
                agent_id=agent_id,
                request_id=request_id,
                session_id=self.session_id,
                event_type=event_type,
                lifecycle=seed_lifecycle(event_type),
                dedupe_key=dedupe_key,
                payload=dict(payload or {}),
                created_at=created_at,
            )
        )

    async def _accept(
        self,
        request_id: str,
        *,
        created_at: float,
        message: str = "hello",
        mode: str = "fg",
        started_by: str = "user",
        parent_request_id: str | None = None,
        agent_id: str = "mode-agent",
    ) -> None:
        await self._append(
            request_id,
            RuntimeEventType.REQUEST_ACCEPTED.value,
            dedupe_key="request.accepted",
            created_at=created_at,
            agent_id=agent_id,
            payload={
                "message": message,
                "request_metadata": {"user_id": "u-1"},
                "mode": mode,
                "started_by": started_by,
                "parent_request_id": parent_request_id,
            },
        )

    async def test_accepted_writes_the_start_columns(self) -> None:
        request_id = f"mode-{uuid.uuid4()}"
        parent_id = f"mode-parent-{uuid.uuid4()}"
        await self._accept(
            request_id,
            created_at=100.0,
            mode="bg",
            started_by="planner",
            parent_request_id=parent_id,
        )

        row = _request_row(self.database_url, request_id)
        self.assertEqual(row["session_id"], self.session_id)
        self.assertEqual(row["mode"], "bg")
        self.assertEqual(row["started_by"], "planner")
        self.assertEqual(row["started_at"], 100.0)
        self.assertEqual(row["parent_request_id"], parent_id)

    async def test_a_later_lifecycle_event_leaves_the_start_columns(self) -> None:
        request_id = f"mode-{uuid.uuid4()}"
        await self._accept(request_id, created_at=100.0, mode="bg")
        await self._append(
            request_id,
            RuntimeEventType.REQUEST_COMPLETED.value,
            dedupe_key="request.completed.0",
            created_at=200.0,
        )

        row = _request_row(self.database_url, request_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["mode"], "bg")
        self.assertEqual(row["started_by"], "user")
        self.assertEqual(row["started_at"], 100.0)
        self.assertEqual(row["session_id"], self.session_id)
        self.assertIsNone(row["parent_request_id"])

    async def test_a_mode_event_changes_only_mode_and_updated_at(self) -> None:
        request_id = f"mode-{uuid.uuid4()}"
        await self._accept(request_id, created_at=100.0)
        before = _request_row(self.database_url, request_id)

        await self._append(
            request_id,
            RuntimeEventType.REQUEST_MODE_CHANGED.value,
            payload={"mode": "bg"},
            created_at=150.0,
        )
        after = _request_row(self.database_url, request_id)
        self.assertEqual(after["mode"], "bg")
        self.assertEqual(after["updated_at"], 150.0)
        for column in ("status", "session_id", "started_by", "started_at", "parent_request_id"):
            self.assertEqual(after[column], before[column], column)

        # A request can switch any number of times; seq orders the switches.
        await self._append(
            request_id,
            RuntimeEventType.REQUEST_MODE_CHANGED.value,
            payload={"mode": "fg"},
            created_at=160.0,
        )
        self.assertEqual(_request_row(self.database_url, request_id)["mode"], "fg")

    async def test_list_session_requests_filters_and_orders(self) -> None:
        older = f"mode-{uuid.uuid4()}"
        newer = f"mode-{uuid.uuid4()}"
        child = f"mode-{uuid.uuid4()}"
        foreground = f"mode-{uuid.uuid4()}"
        await self._accept(older, created_at=100.0, mode="bg", message="older")
        await self._accept(newer, created_at=300.0, mode="bg", message="newer")
        await self._accept(
            child,
            created_at=200.0,
            mode="bg",
            started_by="mode-agent",
            parent_request_id=newer,
            message="child",
        )
        await self._accept(foreground, created_at=400.0, message="fg")
        await self._append(
            older,
            RuntimeEventType.REQUEST_COMPLETED.value,
            dedupe_key="request.completed.0",
            created_at=500.0,
        )

        everything = await self.store.list_session_requests(self.session_id)
        self.assertEqual(
            [r["request_id"] for r in everything], [foreground, newer, child, older]
        )

        background = await self.store.list_session_requests(self.session_id, mode="bg")
        self.assertEqual([r["request_id"] for r in background], [newer, child, older])
        self.assertEqual(
            background[0],
            {
                "request_id": newer,
                "agent_id": "mode-agent",
                "user_id": "u-1",
                "session_id": self.session_id,
                "message": "newer",
                "mode": "bg",
                "status": "running",
                "started_by": "user",
                "parent_request_id": None,
                "started_at": 300.0,
            },
        )

        running_by_user = await self.store.list_session_requests(
            self.session_id, mode="bg", status="running", started_by="user"
        )
        self.assertEqual([r["request_id"] for r in running_by_user], [newer])

    async def test_list_child_requests_filters_by_status(self) -> None:
        parent = f"mode-{uuid.uuid4()}"
        first = f"mode-{uuid.uuid4()}"
        second = f"mode-{uuid.uuid4()}"
        await self._accept(parent, created_at=100.0)
        for created_at, child in ((110.0, first), (120.0, second)):
            await self._accept(
                child,
                created_at=created_at,
                mode="bg",
                started_by="mode-agent",
                parent_request_id=parent,
            )
        await self._append(
            first,
            RuntimeEventType.REQUEST_COMPLETED.value,
            dedupe_key="request.completed.0",
            created_at=130.0,
        )

        children = await self.store.list_child_requests(parent)
        self.assertEqual([c["request_id"] for c in children], [first, second])
        running = await self.store.list_child_requests(parent, status="running")
        self.assertEqual([c["request_id"] for c in running], [second])
        self.assertEqual(await self.store.list_child_requests(first), [])


if __name__ == "__main__":
    unittest.main()

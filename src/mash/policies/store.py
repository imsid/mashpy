"""PolicyStore: the ``policies`` table, read and written by the host."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from mash.storage.pool import acquire_pool, release_pool

from .models import Policy, PolicyRecord

# Tests that use this URL get an in-process store and never open a real
# connection, as with the other host stores.
_TEST_DATABASE_URL = "postgresql://test/runtime"


class _InMemoryPolicyStore:
    def __init__(self) -> None:
        self.records: dict[str, PolicyRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, policy_id: str) -> PolicyRecord | None:
        return self.records.get(policy_id)

    async def put(self, policy_id: str, policy: Policy) -> PolicyRecord:
        async with self._lock:
            now = time.time()
            previous = self.records.get(policy_id)
            record = PolicyRecord(
                policy_id=policy_id,
                policy=policy,
                created_at=previous.created_at if previous is not None else now,
                updated_at=now,
            )
            self.records[policy_id] = record
            return record


def _record(row: dict[str, Any]) -> PolicyRecord:
    return PolicyRecord(
        policy_id=str(row["policy_id"]),
        policy=Policy.model_validate(row["policy"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


_RETURNING = """
    policy_id, policy,
    EXTRACT(EPOCH FROM created_at)::double precision AS created_at,
    EXTRACT(EPOCH FROM updated_at)::double precision AS updated_at
"""


class PolicyStore:
    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._mem: _InMemoryPolicyStore | None = (
            _InMemoryPolicyStore() if resolved == _TEST_DATABASE_URL else None
        )

    async def open(self) -> None:
        if self._mem is not None or self._pool is not None:
            return
        async with self._open_lock:
            if self._pool is None:
                self._pool = await acquire_pool(self._database_url)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool = None
            await release_pool(self._database_url)

    async def get(self, policy_id: str) -> PolicyRecord | None:
        """The policy, or None when there is none with this id."""
        if self._mem is not None:
            return await self._mem.get(policy_id)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_RETURNING} FROM policies WHERE policy_id = %s",
                    (policy_id,),
                )
                row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def put(self, policy_id: str, policy: Policy) -> PolicyRecord:
        """Create the policy, or replace the whole of an existing one."""
        if self._mem is not None:
            return await self._mem.put(policy_id, policy)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    INSERT INTO policies (policy_id, policy)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (policy_id) DO UPDATE
                        SET policy = EXCLUDED.policy, updated_at = now()
                    RETURNING {_RETURNING}
                    """,
                    (policy_id, json.dumps(policy.model_dump())),
                )
                row = await cursor.fetchone()
        return _record(row)

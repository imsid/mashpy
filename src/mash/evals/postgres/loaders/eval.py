"""Read/write loaders for the eval table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...models import Eval


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def row_to_eval(row: dict[str, Any]) -> Eval:
    host_composition = row["host_composition"]
    if isinstance(host_composition, str):
        host_composition = json.loads(host_composition)
    agent_spec_baseline = row["agent_spec_baseline"]
    if isinstance(agent_spec_baseline, str):
        agent_spec_baseline = json.loads(agent_spec_baseline)
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        created_at = datetime.fromisoformat(str(created_at))
    return Eval(
        eval_id=str(row["eval_id"]),
        host_id=str(row["host_id"]),
        user_guidance=str(row.get("user_guidance") or ""),
        host_composition=host_composition,
        agent_spec_baseline=agent_spec_baseline,
        dataset_id=str(row["dataset_id"]),
        rubric_id=str(row["rubric_id"]),
        created_at=created_at.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None
        else created_at,
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def insert_eval(
    pool: Any,
    *,
    eval_id: str,
    host_id: str,
    user_guidance: str,
    host_composition: dict[str, Any],
    agent_spec_baseline: dict[str, Any],
    dataset_id: str,
    rubric_id: str,
) -> Eval:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO eval
                    (eval_id, host_id, user_guidance, host_composition,
                     agent_spec_baseline, dataset_id, rubric_id)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    eval_id,
                    host_id,
                    user_guidance,
                    json.dumps(host_composition),
                    json.dumps(agent_spec_baseline),
                    dataset_id,
                    rubric_id,
                ),
            )
            return row_to_eval(await cursor.fetchone())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_evals(
    pool: Any,
    *,
    host_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Eval]:
    clauses: list[str] = []
    params: list[Any] = []
    if host_id is not None:
        clauses.append("host_id = %s")
        params.append(host_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT * FROM eval {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                tuple(params),
            )
            return [row_to_eval(r) for r in await cursor.fetchall()]


async def get_eval(pool: Any, eval_id: str) -> Eval | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM eval WHERE eval_id = %s", (eval_id,))
            row = await cursor.fetchone()
            return row_to_eval(row) if row else None


async def delete_eval(pool: Any, eval_id: str) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM eval WHERE eval_id = %s", (eval_id,)
            )
            return cursor.rowcount > 0

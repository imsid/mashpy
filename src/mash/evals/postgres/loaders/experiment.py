"""Read/write loaders for the eval_experiment table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...models import Experiment


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def row_to_experiment(row: dict[str, Any]) -> Experiment:
    completed_at = row.get("completed_at")
    return Experiment(
        experiment_id=str(row["experiment_id"]),
        eval_id=str(row["eval_id"]),
        host_composition=_parse_json_object(row.get("host_composition")),
        agent_spec_snapshot=_parse_json_object(row.get("agent_spec_snapshot")),
        status=str(row["status"]),
        created_at=_parse_dt(row["created_at"]),
        completed_at=_parse_dt(completed_at) if completed_at is not None else None,
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def insert_experiment(
    pool: Any,
    *,
    experiment_id: str,
    eval_id: str,
    host_composition: dict[str, Any],
    agent_spec_snapshot: dict[str, Any],
) -> Experiment:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO eval_experiment
                    (experiment_id, eval_id, host_composition, agent_spec_snapshot)
                VALUES (%s, %s, %s::jsonb, %s::jsonb)
                RETURNING *
                """,
                (
                    experiment_id,
                    eval_id,
                    json.dumps(host_composition),
                    json.dumps(agent_spec_snapshot),
                ),
            )
            return row_to_experiment(await cursor.fetchone())


async def update_experiment_status(
    pool: Any,
    experiment_id: str,
    status: str,
    *,
    completed_at: datetime | None = None,
) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE eval_experiment
                SET status = %s, completed_at = %s
                WHERE experiment_id = %s
                """,
                (status, completed_at, experiment_id),
            )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_experiments(
    pool: Any,
    eval_id: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[Experiment]:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT * FROM eval_experiment
                WHERE eval_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (eval_id, limit, offset),
            )
            return [row_to_experiment(r) for r in await cursor.fetchall()]


async def get_experiment(pool: Any, experiment_id: str) -> Experiment | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT * FROM eval_experiment WHERE experiment_id = %s",
                (experiment_id,),
            )
            row = await cursor.fetchone()
            return row_to_experiment(row) if row else None

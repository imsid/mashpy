"""Read/write loaders for the eval_rubric table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...models import ScoringCriterion, ScoringRubric


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def row_to_rubric(row: dict[str, Any]) -> ScoringRubric:
    criteria_raw = row.get("criteria") or []
    if isinstance(criteria_raw, str):
        criteria_raw = json.loads(criteria_raw)
    criteria = tuple(
        ScoringCriterion(
            name=str(c["name"]),
            description=str(c["description"]),
            weight=float(c["weight"]),
            scoring_prompt=str(c["scoring_prompt"]),
            scale_min=int(c.get("scale_min", 1)),
            scale_max=int(c.get("scale_max", 5)),
        )
        for c in criteria_raw
    )
    updated_at = row["updated_at"]
    if not isinstance(updated_at, datetime):
        updated_at = datetime.fromisoformat(str(updated_at))
    return ScoringRubric(
        rubric_id=str(row["rubric_id"]),
        eval_id=str(row["eval_id"]),
        global_scoring_prompt=str(row["global_scoring_prompt"]),
        criteria=criteria,
        updated_at=updated_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None
        else updated_at,
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def insert_rubric(
    pool: Any,
    *,
    rubric_id: str,
    eval_id: str,
    global_scoring_prompt: str,
    criteria: list[dict[str, Any]],
) -> ScoringRubric:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO eval_rubric
                    (rubric_id, eval_id, global_scoring_prompt, criteria)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (rubric_id, eval_id, global_scoring_prompt, json.dumps(criteria)),
            )
            return row_to_rubric(await cursor.fetchone())


async def update_rubric_criteria(
    pool: Any, rubric_id: str, criteria: list[dict[str, Any]]
) -> ScoringRubric:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                UPDATE eval_rubric
                SET criteria = %s::jsonb, updated_at = NOW()
                WHERE rubric_id = %s
                RETURNING *
                """,
                (json.dumps(criteria), rubric_id),
            )
            return row_to_rubric(await cursor.fetchone())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def get_rubric(pool: Any, rubric_id: str) -> ScoringRubric | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT * FROM eval_rubric WHERE rubric_id = %s", (rubric_id,)
            )
            row = await cursor.fetchone()
            return row_to_rubric(row) if row else None

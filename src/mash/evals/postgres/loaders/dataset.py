"""Read/write loaders for the eval_dataset and eval_dataset_row tables."""

from __future__ import annotations

import json
from typing import Any

from ...models import DatasetRow


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def row_to_dataset_row(row: dict[str, Any]) -> DatasetRow:
    target_agents = row.get("target_agents") or []
    if isinstance(target_agents, str):
        target_agents = json.loads(target_agents)
    return DatasetRow(
        row_id=str(row["row_id"]),
        dataset_id=str(row["dataset_id"]),
        input=str(row["input"]),
        scenario_description=str(row["scenario_description"]),
        sampling_category=str(row["sampling_category"]),
        expected_behavior=str(row["expected_behavior"]),
        target_agents=tuple(str(a) for a in target_agents),
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def insert_dataset(pool: Any, *, dataset_id: str, eval_id: str) -> str:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO eval_dataset (dataset_id, eval_id) VALUES (%s, %s)",
                (dataset_id, eval_id),
            )
    return dataset_id


async def insert_dataset_rows(
    pool: Any, dataset_id: str, rows: list[dict[str, Any]]
) -> list[DatasetRow]:
    if not rows:
        return []
    records = []
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            for row in rows:
                target_agents = row.get("target_agents") or []
                await cursor.execute(
                    """
                    INSERT INTO eval_dataset_row
                        (row_id, dataset_id, input, scenario_description,
                         sampling_category, expected_behavior, target_agents)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING *
                    """,
                    (
                        row["row_id"],
                        dataset_id,
                        row["input"],
                        row["scenario_description"],
                        row["sampling_category"],
                        row["expected_behavior"],
                        json.dumps(target_agents),
                    ),
                )
                records.append(row_to_dataset_row(await cursor.fetchone()))
    return records


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def get_dataset_rows(pool: Any, dataset_id: str) -> list[DatasetRow]:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT * FROM eval_dataset_row WHERE dataset_id = %s ORDER BY created_at",
                (dataset_id,),
            )
            return [row_to_dataset_row(r) for r in await cursor.fetchall()]

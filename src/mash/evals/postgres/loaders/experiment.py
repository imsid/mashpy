"""Read/write loaders for the eval_experiment table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...models import Experiment, ExperimentRun


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
        workflow_run_id=(
            str(row["workflow_run_id"])
            if row.get("workflow_run_id") is not None
            else None
        ),
        target_host_id=(
            str(row["target_host_id"])
            if row.get("target_host_id") is not None
            else None
        ),
        rubric_snapshot=_parse_json_object(row.get("rubric_snapshot")),
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
    workflow_run_id: str | None = None,
    target_host_id: str | None = None,
    rubric_snapshot: dict[str, Any] | None = None,
) -> Experiment:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO eval_experiment
                    (experiment_id, eval_id, host_composition, agent_spec_snapshot,
                     workflow_run_id, target_host_id, rubric_snapshot)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    experiment_id,
                    eval_id,
                    json.dumps(host_composition),
                    json.dumps(agent_spec_snapshot),
                    workflow_run_id,
                    target_host_id,
                    json.dumps(rubric_snapshot or {}),
                ),
            )
            return row_to_experiment(await cursor.fetchone())


async def create_experiment_with_runs(
    pool: Any,
    *,
    experiment: Experiment,
    runs: list[ExperimentRun],
) -> Experiment:
    """Create an experiment and its complete row ledger atomically.

    Both experiment and run identities are deterministic, so replay converges
    through ``ON CONFLICT DO NOTHING`` and returns the original experiment.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO eval_experiment
                        (experiment_id, eval_id, host_composition,
                         agent_spec_snapshot, workflow_run_id, target_host_id,
                         rubric_snapshot, status, created_at, completed_at)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                            %s, %s, %s)
                    ON CONFLICT (experiment_id) DO NOTHING
                    """,
                    (
                        experiment.experiment_id,
                        experiment.eval_id,
                        json.dumps(experiment.host_composition),
                        json.dumps(experiment.agent_spec_snapshot),
                        experiment.workflow_run_id,
                        experiment.target_host_id,
                        json.dumps(experiment.rubric_snapshot or {}),
                        experiment.status,
                        experiment.created_at,
                        experiment.completed_at,
                    ),
                )
                for run in runs:
                    await cursor.execute(
                        """
                        INSERT INTO eval_experiment_run
                            (run_id, experiment_id, row_id, input,
                             actual_output, weighted_score, scores, session_id,
                             error, metrics, status, ordinal, created_at,
                             updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                                %s::jsonb, %s, %s, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (
                            run.run_id,
                            run.experiment_id,
                            run.row_id,
                            run.input,
                            run.actual_output,
                            run.weighted_score,
                            json.dumps({}),
                            run.session_id,
                            run.error,
                            json.dumps(run.metrics) if run.metrics is not None else None,
                            run.status,
                            run.ordinal,
                            run.created_at,
                            run.updated_at or run.created_at,
                        ),
                    )
                await cursor.execute(
                    "SELECT * FROM eval_experiment WHERE experiment_id = %s",
                    (experiment.experiment_id,),
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

"""Read/write loaders for the eval_experiment_run table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ...models import CriterionScore, ExperimentRun


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_scores(raw: Any) -> dict[str, CriterionScore]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not raw:
        return {}
    return {
        k: CriterionScore(score=int(v["score"]), rationale=str(v["rationale"]))
        for k, v in raw.items()
    }


def _parse_metrics(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def row_to_run(row: dict[str, Any]) -> ExperimentRun:
    weighted_score = row.get("weighted_score")
    return ExperimentRun(
        run_id=str(row["run_id"]),
        experiment_id=str(row["experiment_id"]),
        row_id=str(row["row_id"]),
        input=str(row["input"]),
        actual_output=str(row["actual_output"]) if row.get("actual_output") is not None else None,
        weighted_score=float(weighted_score) if weighted_score is not None else None,
        scores=_parse_scores(row.get("scores")),
        created_at=_parse_dt(row["created_at"]),
        session_id=str(row["session_id"]) if row.get("session_id") is not None else None,
        error=str(row["error"]) if row.get("error") is not None else None,
        metrics=_parse_metrics(row.get("metrics")),
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def upsert_run(pool: Any, run: ExperimentRun) -> ExperimentRun:
    scores_raw = {
        k: {"score": v.score, "rationale": v.rationale}
        for k, v in run.scores.items()
    }
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO eval_experiment_run
                    (run_id, experiment_id, row_id, input, actual_output,
                     weighted_score, scores, session_id, error, metrics)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                ON CONFLICT (run_id) DO UPDATE SET
                    actual_output  = EXCLUDED.actual_output,
                    weighted_score = EXCLUDED.weighted_score,
                    scores         = EXCLUDED.scores,
                    session_id     = EXCLUDED.session_id,
                    error          = EXCLUDED.error,
                    metrics        = EXCLUDED.metrics
                RETURNING *
                """,
                (
                    run.run_id,
                    run.experiment_id,
                    run.row_id,
                    run.input,
                    run.actual_output,
                    run.weighted_score,
                    json.dumps(scores_raw),
                    run.session_id,
                    run.error,
                    json.dumps(run.metrics) if run.metrics is not None else None,
                ),
            )
            return row_to_run(await cursor.fetchone())


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_runs(
    pool: Any,
    experiment_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[ExperimentRun]:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT * FROM eval_experiment_run
                WHERE experiment_id = %s
                ORDER BY created_at
                LIMIT %s OFFSET %s
                """,
                (experiment_id, limit, offset),
            )
            return [row_to_run(r) for r in await cursor.fetchall()]

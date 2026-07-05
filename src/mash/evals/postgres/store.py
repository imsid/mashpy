"""PostgresEvalStore — connection pool lifecycle and loader delegation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..models import (
    DatasetRow,
    Eval,
    Experiment,
    ExperimentRun,
    ScoringCriterion,
    ScoringRubric,
)
from mash.storage.migrations import run_migrations

from . import loaders

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]


# Recognized in-memory sentinel URL, mirroring PostgresAPIEventStore. Tests
# (and any dev target) that use this URL get an in-process store and never
# open a real connection.
_TEST_DATABASE_URL = "postgresql://test/runtime"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _InMemoryEvalStore:
    """In-memory backend used for the ``postgresql://test/runtime`` sentinel.

    Mirrors PostgresEvalStore's method surface with dict storage so the API
    server can be exercised in tests without a live Postgres.
    """

    def __init__(self) -> None:
        self._evals: dict[str, Eval] = {}
        self._dataset_rows: dict[str, list[DatasetRow]] = {}
        self._rubrics: dict[str, ScoringRubric] = {}
        self._experiments: dict[str, Experiment] = {}
        self._runs: dict[str, ExperimentRun] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self._evals.clear()
        self._dataset_rows.clear()
        self._rubrics.clear()
        self._experiments.clear()
        self._runs.clear()

    # Eval -------------------------------------------------------------

    async def insert_eval(self, **kwargs: Any) -> Eval:
        eval_ = Eval(created_at=_now(), **kwargs)
        self._evals[eval_.eval_id] = eval_
        return eval_

    async def list_evals(
        self,
        *,
        host_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Eval]:
        rows = [
            e
            for e in self._evals.values()
            if host_id is None or e.host_id == host_id
        ]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def get_eval(self, eval_id: str) -> Eval | None:
        return self._evals.get(eval_id)

    async def delete_eval(self, eval_id: str) -> bool:
        return self._evals.pop(eval_id, None) is not None

    # Dataset ----------------------------------------------------------

    async def insert_dataset(self, *, dataset_id: str, eval_id: str) -> str:
        self._dataset_rows.setdefault(dataset_id, [])
        return dataset_id

    async def insert_dataset_rows(
        self, dataset_id: str, rows: list[dict[str, Any]]
    ) -> list[DatasetRow]:
        stored = self._dataset_rows.setdefault(dataset_id, [])
        created: list[DatasetRow] = []
        for row in rows:
            record = DatasetRow(
                row_id=str(row["row_id"]),
                dataset_id=dataset_id,
                input=str(row["input"]),
                scenario_description=str(row["scenario_description"]),
                sampling_category=str(row["sampling_category"]),
                expected_behavior=str(row["expected_behavior"]),
                target_agents=tuple(str(a) for a in (row.get("target_agents") or [])),
            )
            stored.append(record)
            created.append(record)
        return created

    async def get_dataset_rows(self, dataset_id: str) -> list[DatasetRow]:
        return list(self._dataset_rows.get(dataset_id, []))

    # Rubric -----------------------------------------------------------

    async def insert_rubric(
        self,
        *,
        rubric_id: str,
        eval_id: str,
        global_scoring_prompt: str,
        criteria: list[dict[str, Any]],
    ) -> ScoringRubric:
        rubric = ScoringRubric(
            rubric_id=rubric_id,
            eval_id=eval_id,
            global_scoring_prompt=global_scoring_prompt,
            criteria=_criteria_from_dicts(criteria),
            updated_at=_now(),
        )
        self._rubrics[rubric_id] = rubric
        return rubric

    async def get_rubric(self, rubric_id: str) -> ScoringRubric | None:
        return self._rubrics.get(rubric_id)

    async def update_rubric_criteria(
        self, rubric_id: str, criteria: list[dict[str, Any]]
    ) -> ScoringRubric:
        existing = self._rubrics[rubric_id]
        updated = ScoringRubric(
            rubric_id=existing.rubric_id,
            eval_id=existing.eval_id,
            global_scoring_prompt=existing.global_scoring_prompt,
            criteria=_criteria_from_dicts(criteria),
            updated_at=_now(),
        )
        self._rubrics[rubric_id] = updated
        return updated

    # Experiment -------------------------------------------------------

    async def insert_experiment(
        self,
        *,
        experiment_id: str,
        eval_id: str,
        host_composition: dict[str, Any],
        agent_spec_snapshot: dict[str, Any],
    ) -> Experiment:
        experiment = Experiment(
            experiment_id=experiment_id,
            eval_id=eval_id,
            host_composition=host_composition,
            agent_spec_snapshot=agent_spec_snapshot,
            status="pending",
            created_at=_now(),
            completed_at=None,
        )
        self._experiments[experiment_id] = experiment
        return experiment

    async def list_experiments(
        self, eval_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[Experiment]:
        rows = [e for e in self._experiments.values() if e.eval_id == eval_id]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        return self._experiments.get(experiment_id)

    async def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        existing = self._experiments.get(experiment_id)
        if existing is None:
            return
        self._experiments[experiment_id] = Experiment(
            experiment_id=existing.experiment_id,
            eval_id=existing.eval_id,
            host_composition=existing.host_composition,
            agent_spec_snapshot=existing.agent_spec_snapshot,
            status=status,
            created_at=existing.created_at,
            completed_at=completed_at,
        )

    # Experiment Run ---------------------------------------------------

    async def upsert_run(self, run: ExperimentRun) -> ExperimentRun:
        self._runs[run.run_id] = run
        return run

    async def list_runs(
        self, experiment_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ExperimentRun]:
        rows = [r for r in self._runs.values() if r.experiment_id == experiment_id]
        rows.sort(key=lambda r: r.created_at)
        return rows[offset : offset + limit]


def _criteria_from_dicts(
    criteria: list[dict[str, Any]],
) -> tuple[ScoringCriterion, ...]:
    return tuple(
        ScoringCriterion(
            name=str(c["name"]),
            description=str(c.get("description", "")),
            weight=float(c["weight"]),
            scoring_prompt=str(c.get("scoring_prompt", "")),
            scale_min=int(c.get("scale_min", 1)),
            scale_max=int(c.get("scale_max", 5)),
        )
        for c in criteria
    )


class PostgresEvalStore:
    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._mem: _InMemoryEvalStore | None = (
            _InMemoryEvalStore() if resolved == _TEST_DATABASE_URL else None
        )

    async def open(self) -> None:
        if self._mem is not None:
            await self._mem.open()
            return
        if self._pool is not None:
            return
        if dict_row is None or AsyncConnectionPool is None:
            raise RuntimeError(
                "psycopg and psycopg_pool are required for PostgresEvalStore."
            )
        async with self._open_lock:
            if self._pool is not None:
                return
            pool = AsyncConnectionPool(
                self._database_url,
                min_size=1,
                max_size=5,
                open=False,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await pool.open()
            await run_migrations(pool)
            self._pool = pool

    async def close(self) -> None:
        if self._mem is not None:
            await self._mem.close()
            return
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Eval
    # ------------------------------------------------------------------

    async def insert_eval(self, **kwargs: Any) -> Eval:
        await self.open()
        if self._mem is not None:
            return await self._mem.insert_eval(**kwargs)
        return await loaders.insert_eval(self._pool, **kwargs)

    async def list_evals(
        self,
        *,
        host_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Eval]:
        await self.open()
        if self._mem is not None:
            return await self._mem.list_evals(host_id=host_id, limit=limit, offset=offset)
        return await loaders.list_evals(self._pool, host_id=host_id, limit=limit, offset=offset)

    async def get_eval(self, eval_id: str) -> Eval | None:
        await self.open()
        if self._mem is not None:
            return await self._mem.get_eval(eval_id)
        return await loaders.get_eval(self._pool, eval_id)

    async def delete_eval(self, eval_id: str) -> bool:
        await self.open()
        if self._mem is not None:
            return await self._mem.delete_eval(eval_id)
        return await loaders.delete_eval(self._pool, eval_id)

    # ------------------------------------------------------------------
    # Dataset
    # ------------------------------------------------------------------

    async def insert_dataset(self, *, dataset_id: str, eval_id: str) -> str:
        await self.open()
        if self._mem is not None:
            return await self._mem.insert_dataset(dataset_id=dataset_id, eval_id=eval_id)
        return await loaders.insert_dataset(self._pool, dataset_id=dataset_id, eval_id=eval_id)

    async def insert_dataset_rows(
        self, dataset_id: str, rows: list[dict[str, Any]]
    ) -> list[DatasetRow]:
        await self.open()
        if self._mem is not None:
            return await self._mem.insert_dataset_rows(dataset_id, rows)
        return await loaders.insert_dataset_rows(self._pool, dataset_id, rows)

    async def get_dataset_rows(self, dataset_id: str) -> list[DatasetRow]:
        await self.open()
        if self._mem is not None:
            return await self._mem.get_dataset_rows(dataset_id)
        return await loaders.get_dataset_rows(self._pool, dataset_id)

    # ------------------------------------------------------------------
    # Rubric
    # ------------------------------------------------------------------

    async def insert_rubric(self, **kwargs: Any) -> ScoringRubric:
        await self.open()
        if self._mem is not None:
            return await self._mem.insert_rubric(**kwargs)
        return await loaders.insert_rubric(self._pool, **kwargs)

    async def get_rubric(self, rubric_id: str) -> ScoringRubric | None:
        await self.open()
        if self._mem is not None:
            return await self._mem.get_rubric(rubric_id)
        return await loaders.get_rubric(self._pool, rubric_id)

    async def update_rubric_criteria(
        self, rubric_id: str, criteria: list[dict[str, Any]]
    ) -> ScoringRubric:
        await self.open()
        if self._mem is not None:
            return await self._mem.update_rubric_criteria(rubric_id, criteria)
        return await loaders.update_rubric_criteria(self._pool, rubric_id, criteria)

    # ------------------------------------------------------------------
    # Experiment
    # ------------------------------------------------------------------

    async def insert_experiment(self, **kwargs: Any) -> Experiment:
        await self.open()
        if self._mem is not None:
            return await self._mem.insert_experiment(**kwargs)
        return await loaders.insert_experiment(self._pool, **kwargs)

    async def list_experiments(
        self, eval_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[Experiment]:
        await self.open()
        if self._mem is not None:
            return await self._mem.list_experiments(eval_id, limit=limit, offset=offset)
        return await loaders.list_experiments(self._pool, eval_id, limit=limit, offset=offset)

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        await self.open()
        if self._mem is not None:
            return await self._mem.get_experiment(experiment_id)
        return await loaders.get_experiment(self._pool, experiment_id)

    async def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        await self.open()
        if self._mem is not None:
            await self._mem.update_experiment_status(
                experiment_id, status, completed_at=completed_at
            )
            return
        await loaders.update_experiment_status(
            self._pool, experiment_id, status, completed_at=completed_at
        )

    # ------------------------------------------------------------------
    # Experiment Run
    # ------------------------------------------------------------------

    async def upsert_run(self, run: ExperimentRun) -> ExperimentRun:
        await self.open()
        if self._mem is not None:
            return await self._mem.upsert_run(run)
        return await loaders.upsert_run(self._pool, run)

    async def list_runs(
        self, experiment_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ExperimentRun]:
        await self.open()
        if self._mem is not None:
            return await self._mem.list_runs(experiment_id, limit=limit, offset=offset)
        return await loaders.list_runs(self._pool, experiment_id, limit=limit, offset=offset)

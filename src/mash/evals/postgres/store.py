"""PostgresEvalStore — connection pool lifecycle and loader delegation."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..models import DatasetRow, Eval, Experiment, ExperimentRun, ScoringRubric
from . import loaders
from .migrations import run_migrations

try:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:
    dict_row = None  # type: ignore[assignment]
    AsyncConnectionPool = None  # type: ignore[assignment]


class PostgresEvalStore:
    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()

    async def open(self) -> None:
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
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Eval
    # ------------------------------------------------------------------

    async def insert_eval(self, **kwargs: Any) -> Eval:
        await self.open()
        return await loaders.insert_eval(self._pool, **kwargs)

    async def list_evals(
        self,
        *,
        host_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Eval]:
        await self.open()
        return await loaders.list_evals(self._pool, host_id=host_id, limit=limit, offset=offset)

    async def get_eval(self, eval_id: str) -> Eval | None:
        await self.open()
        return await loaders.get_eval(self._pool, eval_id)

    async def delete_eval(self, eval_id: str) -> bool:
        await self.open()
        return await loaders.delete_eval(self._pool, eval_id)

    # ------------------------------------------------------------------
    # Dataset
    # ------------------------------------------------------------------

    async def insert_dataset(self, *, dataset_id: str, eval_id: str) -> str:
        await self.open()
        return await loaders.insert_dataset(self._pool, dataset_id=dataset_id, eval_id=eval_id)

    async def insert_dataset_rows(
        self, dataset_id: str, rows: list[dict[str, Any]]
    ) -> list[DatasetRow]:
        await self.open()
        return await loaders.insert_dataset_rows(self._pool, dataset_id, rows)

    async def get_dataset_rows(self, dataset_id: str) -> list[DatasetRow]:
        await self.open()
        return await loaders.get_dataset_rows(self._pool, dataset_id)

    # ------------------------------------------------------------------
    # Rubric
    # ------------------------------------------------------------------

    async def insert_rubric(self, **kwargs: Any) -> ScoringRubric:
        await self.open()
        return await loaders.insert_rubric(self._pool, **kwargs)

    async def get_rubric(self, rubric_id: str) -> ScoringRubric | None:
        await self.open()
        return await loaders.get_rubric(self._pool, rubric_id)

    async def update_rubric_criteria(
        self, rubric_id: str, criteria: list[dict[str, Any]]
    ) -> ScoringRubric:
        await self.open()
        return await loaders.update_rubric_criteria(self._pool, rubric_id, criteria)

    # ------------------------------------------------------------------
    # Experiment
    # ------------------------------------------------------------------

    async def insert_experiment(self, **kwargs: Any) -> Experiment:
        await self.open()
        return await loaders.insert_experiment(self._pool, **kwargs)

    async def list_experiments(
        self, eval_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[Experiment]:
        await self.open()
        return await loaders.list_experiments(self._pool, eval_id, limit=limit, offset=offset)

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        await self.open()
        return await loaders.get_experiment(self._pool, experiment_id)

    async def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        await self.open()
        await loaders.update_experiment_status(
            self._pool, experiment_id, status, completed_at=completed_at
        )

    # ------------------------------------------------------------------
    # Experiment Run
    # ------------------------------------------------------------------

    async def upsert_run(self, run: ExperimentRun) -> ExperimentRun:
        await self.open()
        return await loaders.upsert_run(self._pool, run)

    async def list_runs(
        self, experiment_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ExperimentRun]:
        await self.open()
        return await loaders.list_runs(self._pool, experiment_id, limit=limit, offset=offset)

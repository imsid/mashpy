"""EvalService — orchestration layer over PostgresEvalStore."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from .models import DatasetRow, Eval, Experiment, ExperimentRun, ScoringRubric
from .postgres.store import PostgresEvalStore


class EvalNotFoundError(LookupError):
    pass


class ExperimentNotFoundError(LookupError):
    pass


class EvalService:
    def __init__(self, store: PostgresEvalStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Evals — read
    # ------------------------------------------------------------------

    async def list_evals(
        self,
        *,
        host_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Eval]:
        return await self._store.list_evals(host_id=host_id, limit=limit, offset=offset)

    async def get_eval_detail(self, eval_id: str) -> dict[str, Any]:
        eval_ = await self._store.get_eval(eval_id)
        if eval_ is None:
            raise EvalNotFoundError(eval_id)
        rows, rubric = await asyncio.gather(
            self._store.get_dataset_rows(eval_.dataset_id),
            self._store.get_rubric(eval_.rubric_id),
        )
        return {
            "eval": eval_to_dict(eval_),
            "rows": [row_to_dict(r) for r in rows],
            "rubric": rubric_to_dict(rubric) if rubric else None,
        }

    # ------------------------------------------------------------------
    # Evals — write
    # ------------------------------------------------------------------

    async def persist_eval(
        self,
        *,
        host_id: str,
        user_guidance: str,
        host_composition: dict[str, Any],
        agent_spec_baseline: dict[str, Any],
        dataset_rows: list[dict[str, Any]],
        rubric: dict[str, Any],
    ) -> Eval:
        eval_id = f"eval_{uuid.uuid4().hex}"
        dataset_id = f"ds_{uuid.uuid4().hex}"
        rubric_id = f"rbr_{uuid.uuid4().hex}"

        # Stamp row_ids for any rows missing them
        for row in dataset_rows:
            if not row.get("row_id"):
                row["row_id"] = f"row_{uuid.uuid4().hex}"

        # eval must exist before dataset/rubric (they FK to eval_id)
        eval_ = await self._store.insert_eval(
            eval_id=eval_id,
            host_id=host_id,
            user_guidance=user_guidance,
            host_composition=host_composition,
            agent_spec_baseline=agent_spec_baseline,
            dataset_id=dataset_id,
            rubric_id=rubric_id,
        )

        await self._store.insert_dataset(dataset_id=dataset_id, eval_id=eval_id)
        await self._store.insert_dataset_rows(dataset_id, dataset_rows)
        await self._store.insert_rubric(
            rubric_id=rubric_id,
            eval_id=eval_id,
            global_scoring_prompt=rubric.get("global_scoring_prompt", ""),
            criteria=rubric.get("criteria", []),
        )

        return eval_

    async def delete_eval(self, eval_id: str) -> bool:
        deleted = await self._store.delete_eval(eval_id)
        if not deleted:
            raise EvalNotFoundError(eval_id)
        return True

    # ------------------------------------------------------------------
    # Rubric
    # ------------------------------------------------------------------

    async def update_rubric(
        self, rubric_id: str, *, criteria: list[dict[str, Any]]
    ) -> ScoringRubric:
        return await self._store.update_rubric_criteria(rubric_id, criteria)

    # ------------------------------------------------------------------
    # Experiments — read
    # ------------------------------------------------------------------

    async def list_experiments(
        self,
        eval_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Experiment]:
        return await self._store.list_experiments(eval_id, limit=limit, offset=offset)

    async def get_experiment_summary(self, experiment_id: str) -> dict[str, Any]:
        experiment = await self._store.get_experiment(experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)
        runs = await self._store.list_runs(experiment_id, limit=1000)
        return {
            "experiment": experiment_to_dict(experiment),
            "aggregate": _compute_aggregate(runs),
        }

    # ------------------------------------------------------------------
    # Experiments — write
    # ------------------------------------------------------------------

    async def persist_experiment(
        self,
        *,
        eval_id: str,
        agent_spec_snapshot: dict[str, Any],
        agent_spec_delta: list[dict[str, Any]],
    ) -> Experiment:
        experiment_id = f"exp_{uuid.uuid4().hex}"
        return await self._store.insert_experiment(
            experiment_id=experiment_id,
            eval_id=eval_id,
            agent_spec_snapshot=agent_spec_snapshot,
            agent_spec_delta=agent_spec_delta,
        )

    async def update_experiment_status(
        self,
        experiment_id: str,
        status: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        await self._store.update_experiment_status(
            experiment_id, status, completed_at=completed_at
        )

    # ------------------------------------------------------------------
    # Experiment Runs
    # ------------------------------------------------------------------

    async def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExperimentRun]:
        return await self._store.list_runs(experiment_id, limit=limit, offset=offset)

    async def persist_run(self, run: ExperimentRun) -> ExperimentRun:
        return await self._store.upsert_run(run)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def eval_to_dict(e: Eval) -> dict[str, Any]:
    return {
        "eval_id": e.eval_id,
        "host_id": e.host_id,
        "user_guidance": e.user_guidance,
        "host_composition": e.host_composition,
        "agent_spec_baseline": e.agent_spec_baseline,
        "dataset_id": e.dataset_id,
        "rubric_id": e.rubric_id,
        "created_at": e.created_at.isoformat(),
    }


def row_to_dict(r: DatasetRow) -> dict[str, Any]:
    return {
        "row_id": r.row_id,
        "dataset_id": r.dataset_id,
        "input": r.input,
        "scenario_description": r.scenario_description,
        "sampling_category": r.sampling_category,
        "expected_behavior": r.expected_behavior,
        "target_agents": list(r.target_agents),
    }


def rubric_to_dict(r: ScoringRubric) -> dict[str, Any]:
    return {
        "rubric_id": r.rubric_id,
        "eval_id": r.eval_id,
        "global_scoring_prompt": r.global_scoring_prompt,
        "criteria": [
            {
                "name": c.name,
                "description": c.description,
                "weight": c.weight,
                "scoring_prompt": c.scoring_prompt,
                "scale_min": c.scale_min,
                "scale_max": c.scale_max,
            }
            for c in r.criteria
        ],
        "updated_at": r.updated_at.isoformat(),
    }


def experiment_to_dict(e: Experiment) -> dict[str, Any]:
    return {
        "experiment_id": e.experiment_id,
        "eval_id": e.eval_id,
        "agent_spec_snapshot": e.agent_spec_snapshot,
        "agent_spec_delta": [
            {
                "agent_id": d.agent_id,
                "system_prompt_changed": d.system_prompt_changed,
                "tools_added": list(d.tools_added),
                "tools_removed": list(d.tools_removed),
                "llm_model_changed": d.llm_model_changed,
                "mcp_servers_added": list(d.mcp_servers_added),
                "mcp_servers_removed": list(d.mcp_servers_removed),
            }
            for d in e.agent_spec_delta
        ],
        "status": e.status,
        "created_at": e.created_at.isoformat(),
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
    }


def _compute_aggregate(runs: list[ExperimentRun]) -> dict[str, Any]:
    scored = [r for r in runs if r.weighted_score is not None]
    if not scored:
        return {"mean_score": None, "by_criterion": {}, "run_count": len(runs)}

    mean_score = sum(r.weighted_score for r in scored) / len(scored)  # type: ignore[arg-type]

    # Aggregate per-criterion scores across all runs
    by_criterion: dict[str, list[int]] = {}
    for run in scored:
        for name, cs in run.scores.items():
            by_criterion.setdefault(name, []).append(cs.score)

    criterion_means = {
        name: sum(scores) / len(scores) for name, scores in by_criterion.items()
    }

    return {
        "mean_score": round(mean_score, 4),
        "by_criterion": {k: round(v, 4) for k, v in criterion_means.items()},
        "run_count": len(runs),
        "scored_count": len(scored),
    }

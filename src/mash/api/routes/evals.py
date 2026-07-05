"""Synthetic evals API routes."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from mash.evals.service import (
    EvalLockedError,
    EvalNotFoundError,
    ExperimentNotFoundError,
    eval_to_dict,
    experiment_to_dict,
    rubric_to_dict,
)

from .common import APIError, get_eval_service, parse_limit, success


class UpdateRubricRequest(BaseModel):
    criteria: list[dict[str, Any]] = Field(min_length=1)


def build_evals_router() -> APIRouter:
    router = APIRouter()

    # ------------------------------------------------------------------
    # Evals
    # ------------------------------------------------------------------

    @router.get("/evals")
    async def list_evals(
        request: Request,
        host_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        resolved_limit = parse_limit(limit, default=50, max_value=200)
        evals = await svc.list_evals(
            host_id=host_id or None,
            limit=resolved_limit,
            offset=max(0, offset),
        )
        return success({"evals": [eval_to_dict(e) for e in evals], "total": len(evals)})

    @router.get("/evals/{eval_id}")
    async def get_eval(request: Request, eval_id: str) -> dict[str, Any]:
        svc = get_eval_service(request)
        try:
            detail = await svc.get_eval_detail(eval_id)
        except EvalNotFoundError:
            raise APIError(
                code="EVAL_NOT_FOUND",
                message=f"eval '{eval_id}' not found",
                status_code=404,
            )
        return success(detail)

    @router.delete("/evals/{eval_id}")
    async def delete_eval(request: Request, eval_id: str) -> dict[str, Any]:
        svc = get_eval_service(request)
        try:
            await svc.delete_eval(eval_id)
        except EvalNotFoundError:
            raise APIError(
                code="EVAL_NOT_FOUND",
                message=f"eval '{eval_id}' not found",
                status_code=404,
            )
        return success({"eval_id": eval_id, "deleted": True})

    # ------------------------------------------------------------------
    # Rubric
    # ------------------------------------------------------------------

    @router.put("/evals/{eval_id}/rubric")
    async def update_rubric(
        request: Request, eval_id: str, body: UpdateRubricRequest
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        try:
            updated = await svc.update_rubric(eval_id, criteria=body.criteria)
        except EvalNotFoundError:
            raise APIError(
                code="EVAL_NOT_FOUND",
                message=f"eval '{eval_id}' not found",
                status_code=404,
            )
        except EvalLockedError:
            raise APIError(
                code="EVAL_LOCKED",
                message=(
                    f"eval '{eval_id}' has experiments and can no longer change; "
                    "generate a new eval to measure something different"
                ),
                status_code=409,
            )
        return success({"rubric": rubric_to_dict(updated)})

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    @router.get("/evals/{eval_id}/experiments")
    async def list_experiments(
        request: Request,
        eval_id: str,
        limit: Optional[int] = Query(default=None),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        resolved_limit = parse_limit(limit, default=20, max_value=100)
        experiments = await svc.list_experiments(
            eval_id, limit=resolved_limit, offset=max(0, offset)
        )
        return success({
            "experiments": [experiment_to_dict(e) for e in experiments],
            "total": len(experiments),
        })

    @router.get("/evals/{eval_id}/experiments/compare")
    async def compare_experiments(
        request: Request,
        eval_id: str,
        baseline: str = Query(),
        control: str = Query(),
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        try:
            comparison = await svc.compare_experiments(
                eval_id, baseline_id=baseline, control_id=control
            )
        except ExperimentNotFoundError as exc:
            raise APIError(
                code="EXPERIMENT_NOT_FOUND",
                message=f"experiment '{exc}' not found for eval '{eval_id}'",
                status_code=404,
            )
        return success(comparison)

    @router.get("/evals/{eval_id}/experiments/{experiment_id}")
    async def get_experiment(
        request: Request, eval_id: str, experiment_id: str
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        try:
            summary = await svc.get_experiment_summary(experiment_id)
        except ExperimentNotFoundError:
            raise APIError(
                code="EXPERIMENT_NOT_FOUND",
                message=f"experiment '{experiment_id}' not found",
                status_code=404,
            )
        return success(summary)

    @router.get("/evals/{eval_id}/experiments/{experiment_id}/runs")
    async def list_runs(
        request: Request,
        eval_id: str,
        experiment_id: str,
        limit: Optional[int] = Query(default=None),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        svc = get_eval_service(request)
        resolved_limit = parse_limit(limit, default=100, max_value=500)
        runs = await svc.list_runs(
            experiment_id, limit=resolved_limit, offset=max(0, offset)
        )
        return success({
            "runs": [_run_to_dict(r) for r in runs],
            "total": len(runs),
        })

    return router


def _run_to_dict(r: Any) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "experiment_id": r.experiment_id,
        "row_id": r.row_id,
        "input": r.input,
        "actual_output": r.actual_output,
        "weighted_score": r.weighted_score,
        "scores": {
            name: {"score": cs.score, "rationale": cs.rationale}
            for name, cs in r.scores.items()
        },
        "created_at": r.created_at.isoformat(),
        "session_id": r.session_id,
        "error": r.error,
        "metrics": r.metrics,
    }

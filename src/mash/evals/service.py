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


class EvalLockedError(RuntimeError):
    """The eval has experiments; its dataset and rubric can no longer change."""


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
        rows, rubric, locked = await asyncio.gather(
            self._store.get_dataset_rows(eval_.dataset_id),
            self._store.get_rubric(eval_.rubric_id),
            self.is_eval_locked(eval_id),
        )
        return {
            "eval": eval_to_dict(eval_),
            "rows": [row_to_dict(r) for r in rows],
            "rubric": rubric_to_dict(rubric) if rubric else None,
            "locked": locked,
        }

    async def is_eval_locked(self, eval_id: str) -> bool:
        """An eval with at least one experiment is locked — derived, not stored."""
        return bool(await self._store.list_experiments(eval_id, limit=1))

    # ------------------------------------------------------------------
    # Evals — write
    # ------------------------------------------------------------------

    async def persist_eval(
        self,
        *,
        host_id: str,
        user_guidance: str,
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
        self, eval_id: str, *, criteria: list[dict[str, Any]]
    ) -> ScoringRubric:
        eval_ = await self._store.get_eval(eval_id)
        if eval_ is None:
            raise EvalNotFoundError(eval_id)
        if await self.is_eval_locked(eval_id):
            raise EvalLockedError(eval_id)
        return await self._store.update_rubric_criteria(eval_.rubric_id, criteria)

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

    async def compare_experiments(
        self, eval_id: str, *, baseline_id: str, control_id: str
    ) -> dict[str, Any]:
        """Read-time comparison of two experiments of the same eval.

        Nothing is stored: the spec delta comes from the two snapshots, the
        aggregates from each experiment's runs, and the row deltas from pairing
        runs by row_id.
        """
        baseline = await self._store.get_experiment(baseline_id)
        if baseline is None or baseline.eval_id != eval_id:
            raise ExperimentNotFoundError(baseline_id)
        control = await self._store.get_experiment(control_id)
        if control is None or control.eval_id != eval_id:
            raise ExperimentNotFoundError(control_id)

        baseline_runs, control_runs = await asyncio.gather(
            self._store.list_runs(baseline_id, limit=1000),
            self._store.list_runs(control_id, limit=1000),
        )
        return {
            "eval_id": eval_id,
            "baseline": {
                "experiment": experiment_to_dict(baseline),
                "aggregate": _compute_aggregate(baseline_runs),
            },
            "control": {
                "experiment": experiment_to_dict(control),
                "aggregate": _compute_aggregate(control_runs),
            },
            "agent_spec_delta": diff_agent_specs(
                baseline.agent_spec_snapshot, control.agent_spec_snapshot
            ),
            "rows": _pair_runs(baseline_runs, control_runs),
        }

    # ------------------------------------------------------------------
    # Experiments — write
    # ------------------------------------------------------------------

    async def persist_experiment(
        self,
        *,
        eval_id: str,
        host_composition: dict[str, Any],
        agent_spec_snapshot: dict[str, Any],
    ) -> Experiment:
        experiment_id = f"exp_{uuid.uuid4().hex}"
        return await self._store.insert_experiment(
            experiment_id=experiment_id,
            eval_id=eval_id,
            host_composition=host_composition,
            agent_spec_snapshot=agent_spec_snapshot,
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
        "host_composition": e.host_composition,
        "agent_spec_snapshot": e.agent_spec_snapshot,
        "status": e.status,
        "created_at": e.created_at.isoformat(),
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
    }


def diff_agent_specs(
    baseline: dict[str, Any], snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Per-agent field-level delta between two agent-spec snapshots.

    Both inputs map ``agent_id -> spec-snapshot`` (see
    ``AgentPool.snapshot_host_agent_specs``). Returns one entry per agent that
    changed, was added, or was removed, listing the fields that differ.
    Computed at comparison time only — deltas are never stored.
    """
    deltas: list[dict[str, Any]] = []
    for agent_id in sorted(set(baseline) | set(snapshot)):
        before = baseline.get(agent_id)
        after = snapshot.get(agent_id)
        if before == after:
            continue
        if before is None:
            deltas.append({"agent_id": agent_id, "change": "added"})
            continue
        if after is None:
            deltas.append({"agent_id": agent_id, "change": "removed"})
            continue
        changed_fields: dict[str, Any] = {}
        for field in sorted(set(before) | set(after)):
            if before.get(field) != after.get(field):
                changed_fields[field] = {
                    "baseline": before.get(field),
                    "snapshot": after.get(field),
                }
        deltas.append(
            {"agent_id": agent_id, "change": "modified", "fields": changed_fields}
        )
    return deltas


def _pair_runs(
    baseline_runs: list[ExperimentRun], control_runs: list[ExperimentRun]
) -> list[dict[str, Any]]:
    """Pair two experiments' runs by row_id and rank by score movement.

    Each row carries the paired runs' details (output, judge scores) so the
    compare view can show them without fetching the run lists again.
    """
    baseline_by_row = {r.row_id: r for r in baseline_runs}
    control_by_row = {r.row_id: r for r in control_runs}
    rows: list[dict[str, Any]] = []
    for row_id in baseline_by_row.keys() | control_by_row.keys():
        b = baseline_by_row.get(row_id)
        c = control_by_row.get(row_id)
        b_score = b.weighted_score if b else None
        c_score = c.weighted_score if c else None
        delta = (
            round(c_score - b_score, 4)
            if b_score is not None and c_score is not None
            else None
        )
        rows.append({
            "row_id": row_id,
            "input": (b or c).input,  # type: ignore[union-attr]
            "baseline_score": b_score,
            "control_score": c_score,
            "delta": delta,
            "baseline": _run_details(b),
            "control": _run_details(c),
        })
    rows.sort(key=lambda r: (r["delta"] is None, -abs(r["delta"] or 0.0)))
    return rows


def _run_details(run: ExperimentRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "actual_output": run.actual_output,
        "scores": {
            name: {"score": cs.score, "rationale": cs.rationale}
            for name, cs in run.scores.items()
        },
        "session_id": run.session_id,
        "error": run.error,
    }


def _compute_aggregate(runs: list[ExperimentRun]) -> dict[str, Any]:
    operational = _compute_operational_aggregate(runs)
    scored = [r for r in runs if r.weighted_score is not None]
    if not scored:
        return {
            "mean_score": None,
            "by_criterion": {},
            "run_count": len(runs),
            "operational": operational,
        }

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
        "operational": operational,
    }


def _compute_operational_aggregate(runs: list[ExperimentRun]) -> dict[str, Any]:
    """Roll up per-row operational metrics across an experiment.

    Read-time so it always matches the persisted rows. Rows without metrics
    (older experiments, or observability disabled) are skipped.
    """
    metricful = [r.metrics for r in runs if isinstance(r.metrics, dict)]
    if not metricful:
        return {"row_count": 0}

    def _sum(key: str) -> int:
        return sum(int(m.get(key) or 0) for m in metricful)

    latencies = [m["latency_ms"] for m in metricful if m.get("latency_ms") is not None]
    tokens = {k: 0 for k in ("input", "output", "cache_read", "cache_creation")}
    for m in metricful:
        tok = m.get("tokens") or {}
        for k in tokens:
            tokens[k] += int(tok.get(k) or 0)

    return {
        "row_count": len(metricful),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "total_tokens": tokens,
        "total_llm_calls": _sum("llm_calls"),
        "total_steps": _sum("steps"),
        "total_tool_calls": _sum("tool_calls"),
        "total_subagent_steps": _sum("num_subagent_steps"),
        "mean_steps": round(_sum("steps") / len(metricful), 2),
    }

"""Tests for EvalService locking and experiment comparison."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from mash.evals.models import CriterionScore, ExperimentRun
from mash.evals.postgres.store import PostgresEvalStore
from mash.evals.service import (
    EvalLockedError,
    EvalService,
    ExperimentNotFoundError,
)

_ROWS = [
    {"row_id": "row-1", "input": "q1", "scenario_description": "s",
     "sampling_category": "random", "expected_behavior": "b", "target_agents": []},
    {"row_id": "row-2", "input": "q2", "scenario_description": "s",
     "sampling_category": "random", "expected_behavior": "b", "target_agents": []},
]

_RUBRIC = {
    "global_scoring_prompt": "Judge it.",
    "criteria": [
        {"name": "accuracy", "description": "d", "weight": 1.0,
         "scoring_prompt": "p", "scale_min": 1, "scale_max": 5},
    ],
}


def _run(coro):
    return asyncio.run(coro)


class _ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EvalService(PostgresEvalStore("postgresql://test/runtime"))

    def _make_eval(self):
        return _run(
            self.service.persist_eval(
                host_id="guide",
                user_guidance="",
                dataset_rows=[dict(r) for r in _ROWS],
                rubric=_RUBRIC,
            )
        )

    def _make_experiment(self, eval_id: str, *, model: str = "m1"):
        return _run(
            self.service.persist_experiment(
                eval_id=eval_id,
                host_composition={"host_id": "guide", "primary": "pilot", "subagents": []},
                agent_spec_snapshot={"pilot": {"model": model, "tools": ["t"]}},
            )
        )

    def _persist_run(self, experiment_id: str, row_id: str, score: float | None):
        run = ExperimentRun(
            run_id=f"run:{experiment_id}:{row_id}",
            experiment_id=experiment_id,
            row_id=row_id,
            input=f"input-{row_id}",
            actual_output="out" if score is not None else None,
            weighted_score=score,
            scores={"accuracy": CriterionScore(score=int(score), rationale="r")}
            if score is not None
            else {},
            created_at=datetime.now(timezone.utc),
            error=None if score is not None else "boom",
        )
        return _run(self.service.persist_run(run))


class EvalLockingTests(_ServiceCase):
    def test_eval_is_unlocked_and_editable_until_first_experiment(self) -> None:
        eval_ = self._make_eval()
        self.assertFalse(_run(self.service.is_eval_locked(eval_.eval_id)))
        updated = _run(
            self.service.update_rubric(eval_.eval_id, criteria=_RUBRIC["criteria"])
        )
        self.assertEqual(updated.rubric_id, eval_.rubric_id)
        detail = _run(self.service.get_eval_detail(eval_.eval_id))
        self.assertFalse(detail["locked"])

    def test_first_experiment_locks_the_eval(self) -> None:
        eval_ = self._make_eval()
        self._make_experiment(eval_.eval_id)
        self.assertTrue(_run(self.service.is_eval_locked(eval_.eval_id)))
        detail = _run(self.service.get_eval_detail(eval_.eval_id))
        self.assertTrue(detail["locked"])
        with self.assertRaises(EvalLockedError):
            _run(self.service.update_rubric(eval_.eval_id, criteria=_RUBRIC["criteria"]))


class ExperimentLedgerTests(_ServiceCase):
    def test_prepare_experiment_is_idempotent_and_seeds_rows(self) -> None:
        eval_ = self._make_eval()
        kwargs = {
            "experiment_id": "exp_workflow_1",
            "workflow_run_id": "workflow-1",
            "eval_id": eval_.eval_id,
            "target_host_id": "guide",
            "host_composition": {
                "host_id": "guide",
                "primary": "pilot",
                "subagents": [],
            },
            "agent_spec_snapshot": {"pilot": {"model": "m1"}},
            "rubric_snapshot": dict(_RUBRIC),
            "rows": [dict(row) for row in _ROWS],
        }

        first = _run(self.service.prepare_experiment(**kwargs))
        second = _run(self.service.prepare_experiment(**kwargs))

        self.assertEqual(first.experiment_id, second.experiment_id)
        self.assertEqual(first.workflow_run_id, "workflow-1")
        self.assertEqual(first.target_host_id, "guide")
        self.assertEqual(first.rubric_snapshot, _RUBRIC)
        runs = _run(self.service.list_runs(first.experiment_id, limit=1000))
        self.assertEqual(len(runs), 2)
        self.assertEqual([run.ordinal for run in runs], [0, 1])
        self.assertTrue(all(run.status == "pending" for run in runs))
        self.assertEqual(
            [run.session_id for run in runs],
            ["eval:workflow-1:row-1", "eval:workflow-1:row-2"],
        )


class CompareExperimentsTests(_ServiceCase):
    def test_compare_diffs_snapshots_and_pairs_rows(self) -> None:
        eval_ = self._make_eval()
        baseline = self._make_experiment(eval_.eval_id, model="m1")
        control = self._make_experiment(eval_.eval_id, model="m2")
        self._persist_run(baseline.experiment_id, "row-1", 2.0)
        self._persist_run(baseline.experiment_id, "row-2", 4.0)
        self._persist_run(control.experiment_id, "row-1", 5.0)
        self._persist_run(control.experiment_id, "row-2", None)  # errored row

        comparison = _run(
            self.service.compare_experiments(
                eval_.eval_id,
                baseline_id=baseline.experiment_id,
                control_id=control.experiment_id,
            )
        )

        delta = comparison["agent_spec_delta"]
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["agent_id"], "pilot")
        self.assertEqual(delta[0]["change"], "modified")
        self.assertIn("model", delta[0]["fields"])

        self.assertAlmostEqual(
            comparison["baseline"]["aggregate"]["mean_score"], 3.0
        )
        self.assertAlmostEqual(
            comparison["control"]["aggregate"]["mean_score"], 5.0
        )

        rows = comparison["rows"]
        self.assertEqual([r["row_id"] for r in rows], ["row-1", "row-2"])
        self.assertAlmostEqual(rows[0]["delta"], 3.0)
        # a row unscored in one experiment pairs with a null delta, ranked last
        self.assertIsNone(rows[1]["delta"])
        self.assertIsNone(rows[1]["control_score"])
        self.assertAlmostEqual(rows[1]["baseline_score"], 4.0)

        # each side carries the run's output and judge scores for the drawer
        self.assertEqual(rows[0]["baseline"]["actual_output"], "out")
        self.assertEqual(
            rows[0]["baseline"]["scores"],
            {"accuracy": {"score": 2, "rationale": "r"}},
        )
        self.assertEqual(
            rows[0]["control"]["scores"],
            {"accuracy": {"score": 5, "rationale": "r"}},
        )
        # an errored row surfaces the failure reason on its side
        self.assertIsNone(rows[1]["control"]["actual_output"])
        self.assertEqual(rows[1]["control"]["error"], "boom")

    def test_compare_rejects_experiment_from_another_eval(self) -> None:
        eval_a = self._make_eval()
        eval_b = self._make_eval()
        exp_a = self._make_experiment(eval_a.eval_id)
        exp_b = self._make_experiment(eval_b.eval_id)
        with self.assertRaises(ExperimentNotFoundError):
            _run(
                self.service.compare_experiments(
                    eval_a.eval_id,
                    baseline_id=exp_a.experiment_id,
                    control_id=exp_b.experiment_id,
                )
            )


if __name__ == "__main__":
    unittest.main()

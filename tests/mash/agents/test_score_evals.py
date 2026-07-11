"""Tests for the ordinary run-experiment workflow and judge contract."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import patch

from mash.agents.masher.context import MasherRuntimeContext
from mash.agents.masher.judge import JudgeError, build_judge_message, parse_judge_output
from mash.agents.masher.workflows import (
    RunExperimentInput,
    build_run_experiment_workflow,
)
from mash.evals.postgres.store import PostgresEvalStore
from mash.evals.service import EvalService, diff_agent_specs
from mash.runtime.host.types import Host
from mash.workflows import CodeStep, StepContext

_RUBRIC = {
    "global_scoring_prompt": "Judge helpfulness.",
    "criteria": [
        {
            "name": "accuracy",
            "description": "Correctness",
            "scoring_prompt": "Is it correct?",
            "weight": 0.6,
            "scale_min": 1,
            "scale_max": 5,
        },
        {
            "name": "clarity",
            "description": "Clarity",
            "scoring_prompt": "Is it clear?",
            "weight": 0.4,
            "scale_min": 1,
            "scale_max": 5,
        },
    ],
}

_ROWS = [
    {
        "row_id": "0",
        "input": "q0",
        "scenario_description": "s",
        "sampling_category": "random",
        "expected_behavior": "b",
        "target_agents": [],
    },
    {
        "row_id": "1",
        "input": "q1",
        "scenario_description": "s",
        "sampling_category": "random",
        "expected_behavior": "b",
        "target_agents": [],
    },
]


class JudgeTests(unittest.TestCase):
    def test_build_judge_message_includes_rubric_and_row(self) -> None:
        msg = build_judge_message(
            row_input="what is 2+2?", actual_output="4", rubric=_RUBRIC
        )
        self.assertIn("Judge helpfulness.", msg)
        self.assertIn("accuracy", msg)
        self.assertIn("clarity", msg)
        self.assertIn("what is 2+2?", msg)
        self.assertIn("4", msg)

    def test_build_judge_message_marks_missing_output(self) -> None:
        msg = build_judge_message(row_input="x", actual_output=None, rubric=_RUBRIC)
        self.assertIn("no output", msg)

    def test_parse_recomputes_weighted_score_from_rubric(self) -> None:
        json_text = json.dumps(
            {
                "scores": {
                    "accuracy": {"score": 5, "rationale": "correct"},
                    "clarity": {"score": 3, "rationale": "ok"},
                    "clarity_extra": {"score": 99},
                },
                "weighted_score": 999,
            }
        )
        scores, weighted = parse_judge_output(json_text, _RUBRIC)
        self.assertEqual(set(scores), {"accuracy", "clarity"})
        self.assertAlmostEqual(weighted, 0.6 * 5 + 0.4 * 3)

    def test_parse_clamps_score_to_scale(self) -> None:
        json_text = json.dumps(
            {
                "scores": {
                    "accuracy": {"score": 42, "rationale": "hi"},
                    "clarity": {"score": -3, "rationale": "lo"},
                }
            }
        )
        scores, _ = parse_judge_output(json_text, _RUBRIC)
        self.assertEqual(scores["accuracy"].score, 5)
        self.assertEqual(scores["clarity"].score, 1)

    def test_parse_raises_on_missing_criterion(self) -> None:
        with self.assertRaises(JudgeError):
            parse_judge_output(
                json.dumps({"scores": {"accuracy": {"score": 4, "rationale": "x"}}}),
                _RUBRIC,
            )


class DiffAgentSpecsTests(unittest.TestCase):
    def test_detects_added_removed_modified(self) -> None:
        baseline = {"a": {"model": "m1", "tools": ["x"]}, "gone": {"model": "m0"}}
        snapshot = {"a": {"model": "m2", "tools": ["x"]}, "new": {"model": "m9"}}
        deltas = {d["agent_id"]: d for d in diff_agent_specs(baseline, snapshot)}
        self.assertEqual(deltas["gone"]["change"], "removed")
        self.assertEqual(deltas["new"]["change"], "added")
        self.assertEqual(deltas["a"]["change"], "modified")
        self.assertIn("model", deltas["a"]["fields"])


class _FakeRuntimeStore:
    async def list_session_events(
        self, session_id: str, *, event_types: Any = None
    ) -> list[Any]:
        del event_types
        from mash.runtime.events.types import RuntimeEvent

        return [
            RuntimeEvent(
                app_id="pilot",
                agent_id="pilot",
                event_type="runtime.step.completed",
                session_id=session_id,
                created_at=1.0,
            ),
            RuntimeEvent(
                app_id="pilot",
                agent_id="pilot",
                event_type="llm.request.complete",
                session_id=session_id,
                created_at=2.0,
                payload={"input_tokens": 10, "output_tokens": 5},
            ),
        ]


class _FakePool:
    runner_id = "runner-1"

    def get_host(self, host_id: str) -> Host:
        if host_id != "guide":
            raise ValueError(host_id)
        return Host(host_id="guide", primary="pilot")

    def snapshot_for(self, host: Host) -> dict[str, Any]:
        return {"host_id": host.host_id, "primary": host.primary, "subagents": []}

    def snapshot_host_agent_specs(self, host_id: str) -> dict[str, Any]:
        del host_id
        return {"pilot": {"model": "m1", "tools": ["t"]}}

    def get_runtime_store(self) -> _FakeRuntimeStore:
        return _FakeRuntimeStore()


def _host_payload(text: str) -> dict[str, Any]:
    return {"response": {"text": text}}


def _judge_payload(scores: dict[str, int]) -> dict[str, Any]:
    body = {
        "scores": {
            name: {"score": score, "rationale": "r"}
            for name, score in scores.items()
        }
    }
    return {"response": {"structured_output": {"json_text": json.dumps(body)}}}


class RunExperimentWorkflowTests(unittest.TestCase):
    def _run(
        self,
        *,
        judge_map: dict[str, dict[str, int] | None],
        host_failures: set[str] | None = None,
    ) -> tuple[Any, EvalService, list[str]]:
        async def scenario() -> tuple[Any, EvalService, list[str]]:
            service = EvalService(PostgresEvalStore("postgresql://test/runtime"))
            eval_ = await service.persist_eval(
                host_id="guide",
                user_guidance="",
                dataset_rows=[dict(row) for row in _ROWS],
                rubric=_RUBRIC,
            )
            context = MasherRuntimeContext(
                runtime_store=_FakeRuntimeStore(),
                eval_service=service,
                pool=_FakePool(),
            )
            workflow = build_run_experiment_workflow(context)
            self.assertEqual([step.kind for step in workflow.steps], ["code"] * 3)
            calls: list[str] = []

            async def fake_post(_runner_id: str, *, task_id: str, **_kwargs: Any) -> str:
                calls.append(task_id)
                return f"req:{task_id}"

            async def fake_collect(
                _runner_id: str, _agent_id: str, request_id: str
            ) -> dict[str, Any]:
                _, phase, row_id = request_id.split(":")
                if phase == "execute":
                    if row_id in (host_failures or set()):
                        raise RuntimeError(f"host failed {row_id}")
                    return _host_payload(f"out-{row_id}")
                scores = judge_map.get(row_id)
                if scores is None:
                    return {"response": {"structured_output": {"json_text": "bad"}}}
                return _judge_payload(scores)

            with patch(
                "mash.agents.masher.workflows.post_inline_agent_request", fake_post
            ), patch(
                "mash.agents.masher.workflows.collect_terminal_payload", fake_collect
            ):
                first, second, third = workflow.steps
                assert isinstance(first, CodeStep)
                assert isinstance(second, CodeStep)
                assert isinstance(third, CodeStep)
                ref = await first.run(
                    RunExperimentInput(eval_id=eval_.eval_id, host_id="guide"),
                    StepContext(
                        run_id="workflow-run-1",
                        step_id="prepare-experiment",
                        workflow_input={},
                    ),
                )
                ref = await second.run(
                    ref,
                    StepContext(
                        run_id="workflow-run-1",
                        step_id="execute-rows",
                        workflow_input={},
                    ),
                )
                result = await third.run(
                    ref,
                    StepContext(
                        run_id="workflow-run-1",
                        step_id="judge-rows",
                        workflow_input={},
                    ),
                )
            return result, service, calls

        return asyncio.run(scenario())

    def test_scores_rows_and_snapshots_experiment(self) -> None:
        result, service, calls = self._run(
            judge_map={
                "0": {"accuracy": 5, "clarity": 5},
                "1": {"accuracy": 1, "clarity": 1},
            }
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.scored_count, 2)
        self.assertAlmostEqual(result.mean_score or 0, 3.0)
        self.assertEqual(
            calls,
            ["execute:0", "execute:1", "judge:0", "judge:1"],
        )
        experiment = asyncio.run(service.get_experiment(result.experiment_id))
        self.assertEqual(experiment.target_host_id, "guide")
        self.assertEqual(experiment.host_composition["primary"], "pilot")
        assert experiment.rubric_snapshot is not None
        self.assertEqual(
            experiment.rubric_snapshot["global_scoring_prompt"],
            _RUBRIC["global_scoring_prompt"],
        )
        self.assertEqual(
            experiment.rubric_snapshot["criteria"], _RUBRIC["criteria"]
        )
        runs = asyncio.run(service.list_runs(result.experiment_id, limit=1000))
        self.assertTrue(all(run.status == "scored" for run in runs))
        self.assertTrue(all(run.metrics is not None for run in runs))

    def test_host_and_judge_failures_are_isolated(self) -> None:
        result, service, calls = self._run(
            judge_map={"0": {"accuracy": 4, "clarity": 4}},
            host_failures={"1"},
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.scored_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertNotIn("judge:1", calls)
        runs = asyncio.run(service.list_runs(result.experiment_id, limit=1000))
        by_row = {run.row_id: run for run in runs}
        self.assertEqual(by_row["0"].status, "scored")
        self.assertEqual(by_row["1"].status, "execution_failed")
        self.assertIn("host failed", by_row["1"].error or "")

    def test_judge_failure_keeps_host_output(self) -> None:
        result, service, _ = self._run(
            judge_map={"0": {"accuracy": 4, "clarity": 4}, "1": None}
        )
        self.assertEqual(result.scored_count, 1)
        runs = asyncio.run(service.list_runs(result.experiment_id, limit=1000))
        failed = next(run for run in runs if run.row_id == "1")
        self.assertEqual(failed.status, "scoring_failed")
        self.assertEqual(failed.actual_output, "out-1")
        self.assertIsNotNone(failed.error)


if __name__ == "__main__":
    unittest.main()

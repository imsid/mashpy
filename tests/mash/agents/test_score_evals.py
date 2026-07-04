"""Tests for the durable, parallel score-evals workflow."""

from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from typing import Any
from unittest.mock import patch

from mash.agents.masher import score_runner
from mash.agents.masher.judge import JudgeError, build_judge_message, parse_judge_output
from mash.agents.masher.score_runner import ScoreEvalsStrategy
from mash.evals.service import diff_agent_specs
from mash.workflows.strategy import WorkflowExecutionContext

_RUBRIC = {
    "global_scoring_prompt": "Judge helpfulness.",
    "criteria": [
        {"name": "accuracy", "scoring_prompt": "Is it correct?", "weight": 0.6,
         "scale_min": 1, "scale_max": 5},
        {"name": "clarity", "scoring_prompt": "Is it clear?", "weight": 0.4,
         "scale_min": 1, "scale_max": 5},
    ],
}


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
                    # a bogus weighted_score the model might emit is ignored
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
            {"scores": {"accuracy": {"score": 42, "rationale": "hi"},
                        "clarity": {"score": -3, "rationale": "lo"}}}
        )
        scores, _ = parse_judge_output(json_text, _RUBRIC)
        self.assertEqual(scores["accuracy"].score, 5)
        self.assertEqual(scores["clarity"].score, 1)

    def test_parse_raises_on_missing_criterion(self) -> None:
        json_text = json.dumps({"scores": {"accuracy": {"score": 4, "rationale": "x"}}})
        with self.assertRaises(JudgeError):
            parse_judge_output(json_text, _RUBRIC)

    def test_parse_raises_on_invalid_json(self) -> None:
        with self.assertRaises(JudgeError):
            parse_judge_output("not json", _RUBRIC)


class DiffAgentSpecsTests(unittest.TestCase):
    def test_detects_added_removed_modified(self) -> None:
        baseline = {
            "a": {"model": "m1", "tools": ["x"]},
            "gone": {"model": "m0"},
        }
        snapshot = {
            "a": {"model": "m2", "tools": ["x"]},
            "new": {"model": "m9"},
        }
        deltas = {d["agent_id"]: d for d in diff_agent_specs(baseline, snapshot)}
        self.assertEqual(deltas["gone"]["change"], "removed")
        self.assertEqual(deltas["new"]["change"], "added")
        self.assertEqual(deltas["a"]["change"], "modified")
        self.assertIn("model", deltas["a"]["fields"])
        self.assertNotIn("tools", deltas["a"]["fields"])

    def test_identical_specs_produce_no_delta(self) -> None:
        spec = {"a": {"model": "m1"}}
        self.assertEqual(diff_agent_specs(spec, dict(spec)), [])


# --- Strategy fan-out with fakes -------------------------------------------


class _FakeDBOS:
    async def run_step_async(self, _config: dict, fn: Any, *args: Any) -> Any:
        return await fn(*args)


@contextlib.contextmanager
def _fake_set_workflow_id(_wf_id: str):
    yield


def _fake_load_dbos_api():
    return _FakeDBOS(), None, _fake_set_workflow_id, None, None


class _FakeHandle:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def get_result(self) -> Any:
        return self._result


class _FakeQueue:
    async def enqueue_async(self, workflow: Any, *args: Any) -> _FakeHandle:
        return _FakeHandle(await workflow(*args))


class _FakeHost:
    primary = "pilot"


class _FakeRuntimeStore:
    async def list_session_events(self, session_id: str, *, event_types: Any = None) -> list:
        from mash.runtime.events.types import RuntimeEvent

        return [
            RuntimeEvent(
                app_id="pilot", agent_id="pilot", event_type="runtime.step.completed",
                session_id=session_id, created_at=1.0,
            ),
            RuntimeEvent(
                app_id="pilot", agent_id="pilot", event_type="llm.request.complete",
                session_id=session_id, created_at=2.0,
                payload={"input_tokens": 10, "output_tokens": 5, "finish_reason": "end_turn"},
            ),
        ]


class _FakePool:
    def get_host(self, _host_id: str) -> _FakeHost:
        return _FakeHost()

    def get_runtime_store(self) -> _FakeRuntimeStore:
        return _FakeRuntimeStore()

    def snapshot_for(self, _host: Any) -> dict:
        return {"host_id": "guide", "primary": "pilot", "subagents": []}

    def snapshot_host_agent_specs(self, _host_id: str) -> dict:
        return {"pilot": {"model": "m1", "tools": ["t"]}}


class _FakeExperiment:
    experiment_id = "exp_1"


class _FakeService:
    def __init__(self, detail: dict) -> None:
        self._detail = detail
        self.persisted_runs: list[Any] = []
        self.experiment_kwargs: dict[str, Any] | None = None
        self.status: tuple[str, Any] | None = None

    async def get_eval_detail(self, _eval_id: str) -> dict:
        return self._detail

    async def persist_experiment(self, **kwargs: Any) -> _FakeExperiment:
        self.experiment_kwargs = kwargs
        return _FakeExperiment()

    async def persist_run(self, run: Any) -> None:
        self.persisted_runs.append(run)

    async def update_experiment_status(self, _eid: str, status: str, **_kw: Any) -> None:
        self.status = (status, _kw.get("completed_at"))


class _FakeContext:
    def __init__(self, service: _FakeService) -> None:
        self._service = service

    def require_eval_service(self) -> _FakeService:
        return self._service


def _host_payload(text: str) -> dict:
    return {"response": {"text": text}}


def _judge_payload(scores: dict[str, int]) -> dict:
    body = {"scores": {n: {"score": s, "rationale": "r"} for n, s in scores.items()}}
    return {"response": {"structured_output": {"json_text": json.dumps(body)}}}


class ScoreEvalsStrategyTests(unittest.TestCase):
    def _run(self, detail: dict, judge_map: dict[str, dict[str, int] | None]) -> Any:
        service = _FakeService(detail)
        strategy = ScoreEvalsStrategy(context=_FakeContext(service))

        async def fake_post(_runner_id, *, agent_id, task_id, **_kwargs):
            return f"req:{task_id}"

        async def fake_collect(_runner_id, _agent_id, request_id):
            if "host" in request_id:
                row_id = request_id.split(":")[-1]
                return _host_payload(f"out-{row_id}")
            row_id = request_id.split(":")[-1]
            scores = judge_map.get(row_id)
            if scores is None:
                return {"response": {"structured_output": {"json_text": "bad json"}}}
            return _judge_payload(scores)

        ctx = WorkflowExecutionContext(
            runner_id="r1",
            workflow=object(),  # unused by the strategy
            run_id="run1",
            workflow_input={"eval_id": "eval_1"},
        )
        with patch.multiple(
            score_runner,
            load_dbos_api=_fake_load_dbos_api,
            require_runner=lambda _r: _FakePool(),
            post_inline_agent_request=fake_post,
            collect_terminal_payload=fake_collect,
        ):
            score_runner._STATE.queue = _FakeQueue()
            score_runner._STATE.workflow = score_runner._score_row
            return asyncio.run(strategy.run(ctx)), service

    def test_scores_all_rows_and_computes_mean(self) -> None:
        detail = {
            "eval": {"host_id": "guide"},
            "rows": [
                {"row_id": "0", "input": "q0"},
                {"row_id": "1", "input": "q1"},
            ],
            "rubric": _RUBRIC,
        }
        result, service = self._run(
            detail,
            judge_map={"0": {"accuracy": 5, "clarity": 5}, "1": {"accuracy": 1, "clarity": 1}},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scored_count"], 2)
        # row0 weighted 5.0, row1 weighted 1.0 -> mean 3.0
        self.assertAlmostEqual(result["mean_score"], 3.0)
        self.assertEqual(len(service.persisted_runs), 2)
        # the experiment records the live host state that was evaluated
        self.assertEqual(
            service.experiment_kwargs["host_composition"],
            {"host_id": "guide", "primary": "pilot", "subagents": []},
        )
        self.assertEqual(
            service.experiment_kwargs["agent_spec_snapshot"],
            {"pilot": {"model": "m1", "tools": ["t"]}},
        )
        # every persisted run carries operational metrics aggregated from its session
        for run in service.persisted_runs:
            self.assertIsNotNone(run.metrics)
            self.assertEqual(run.metrics["tokens"]["input"], 10)
            self.assertEqual(run.metrics["steps"], 1)

    def test_failed_row_does_not_fail_the_run(self) -> None:
        detail = {
            "eval": {"host_id": "guide"},
            "rows": [
                {"row_id": "0", "input": "q0"},
                {"row_id": "1", "input": "q1"},
            ],
            "rubric": _RUBRIC,
        }
        result, service = self._run(
            detail,
            judge_map={"0": {"accuracy": 4, "clarity": 4}, "1": None},  # row 1 judge fails
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scored_count"], 1)
        self.assertAlmostEqual(result["mean_score"], 4.0)
        # both rows persisted, one with a null weighted_score
        self.assertEqual(len(service.persisted_runs), 2)
        nulls = [r for r in service.persisted_runs if r.weighted_score is None]
        self.assertEqual(len(nulls), 1)
        # the failed row carries its failure reason instead of a blank score
        self.assertIsNotNone(nulls[0].error)
        # the scored row has no error
        scored = [r for r in service.persisted_runs if r.weighted_score is not None]
        self.assertIsNone(scored[0].error)

    def test_missing_eval_id_fails_fast(self) -> None:
        service = _FakeService({})
        strategy = ScoreEvalsStrategy(context=_FakeContext(service))
        ctx = WorkflowExecutionContext(
            runner_id="r1", workflow=object(), run_id="run1", workflow_input={}
        )
        with patch.multiple(
            score_runner,
            load_dbos_api=_fake_load_dbos_api,
            require_runner=lambda _r: _FakePool(),
        ):
            result = asyncio.run(strategy.run(ctx))
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()

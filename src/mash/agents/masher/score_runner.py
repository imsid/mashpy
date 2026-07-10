"""Durable, parallel scoring orchestration for the ``score-evals`` workflow.

This is a :class:`WorkflowStrategy` that replaces the generic sequential task
loop for ``score-evals``. It loads the eval, snapshots the host under test,
fans out one durable child workflow per dataset row over a dedicated DBOS queue
(run the row through the host, then judge the output with the eval agent), and persists
the gathered results as an experiment. Orchestration is deterministic code;
the eval agent is used only as the per-row judge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mash.workflows.dbos import (
    collect_terminal_payload,
    load_dbos_api,
    post_inline_agent_request,
    require_runner,
)
from mash.workflows.strategy import WorkflowExecutionContext, WorkflowStrategy

from ...evals.metrics import METRIC_EVENT_TYPES, compute_row_metrics
from ...evals.models import CriterionScore, ExperimentRun
from .context import MasherRuntimeContext
from .judge import EVAL_JUDGE_STRUCTURED_OUTPUT, build_judge_message, parse_judge_output
from .spec import EVAL_AGENT_ID

_ROW_QUEUE_NAME = "mash.eval.rows"
_DEFAULT_ROW_CONCURRENCY = 8
_WORKFLOW_ID = "score-evals"


class _ScoreRowState:
    queue: Any = None
    workflow: Any = None


_STATE = _ScoreRowState()


async def _collect_row_metrics(
    runner_id: str, session_id: str, primary_agent_id: str
) -> dict[str, Any] | None:
    """Aggregate operational metrics from the row's host session events.

    Runs regardless of whether the host request succeeded — the events are
    logged either way, so an errored row still reports the tokens/steps it spent
    before failing. Never raises: metrics must not fail a row.
    """
    try:
        pool = require_runner(runner_id)
        store = pool.get_runtime_store()
        if store is None:
            return None
        events = await store.list_session_events(
            session_id, event_types=list(METRIC_EVENT_TYPES)
        )
        metrics = compute_row_metrics(events, primary_agent_id=primary_agent_id)
        return metrics.to_dict()
    except Exception:  # noqa: BLE001 - operational metrics are best-effort
        return None


def _response_text(payload: dict[str, Any]) -> str | None:
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    text = response.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _response_json_text(payload: dict[str, Any]) -> str:
    response = payload.get("response")
    structured = response.get("structured_output") if isinstance(response, dict) else None
    if not isinstance(structured, dict) or not isinstance(structured.get("json_text"), str):
        raise ValueError("judge response did not return json_text")
    return structured["json_text"]


async def _score_row(
    runner_id: str,
    run_id: str,
    host_id: str,
    host_snapshot: dict[str, Any],
    primary_agent_id: str,
    row: dict[str, Any],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Child DBOS workflow: run one row through the host, then judge it."""
    del host_id  # unused here; kept in the durable arg record for observability
    dbos_class, _, _, _, _ = load_dbos_api()
    row_id = str(row.get("row_id") or "")
    row_input = str(row.get("input") or "")
    session_id = f"eval:{run_id}:{row_id}"
    scored: dict[str, Any] = {
        "row_id": row_id,
        "input": row_input,
        "actual_output": None,
        "weighted_score": None,
        "scores": {},
        "status": "failed",
        "session_id": session_id,
        "metrics": None,
    }
    try:
        # 1) Run the row input through the host under test (full composition).
        host_request = await post_inline_agent_request(
            runner_id,
            agent_id=primary_agent_id,
            message=row_input,
            structured_output=None,
            workflow_id=_WORKFLOW_ID,
            workflow_run_id=run_id,
            task_id=f"score-row-host:{row_id}",
            session_id=session_id,
            host_snapshot=host_snapshot,
        )
        host_payload = await dbos_class.run_step_async(
            {"name": f"score-row.host.{row_id}"},
            collect_terminal_payload,
            runner_id,
            primary_agent_id,
            host_request,
        )
        actual_output = _response_text(host_payload)
        scored["actual_output"] = actual_output

        # 2) Judge the output with the eval agent (structured output).
        judge_request = await post_inline_agent_request(
            runner_id,
            agent_id=EVAL_AGENT_ID,
            message=build_judge_message(
                row_input=row_input, actual_output=actual_output, rubric=rubric
            ),
            structured_output=EVAL_JUDGE_STRUCTURED_OUTPUT,
            workflow_id=_WORKFLOW_ID,
            workflow_run_id=run_id,
            task_id=f"score-row-judge:{row_id}",
            session_id=session_id,
        )
        judge_payload = await dbos_class.run_step_async(
            {"name": f"score-row.judge.{row_id}"},
            collect_terminal_payload,
            runner_id,
            EVAL_AGENT_ID,
            judge_request,
        )
        scores, weighted_score = parse_judge_output(
            _response_json_text(judge_payload), rubric
        )
        scored["scores"] = {
            name: {"score": cs.score, "rationale": cs.rationale}
            for name, cs in scores.items()
        }
        scored["weighted_score"] = weighted_score
        scored["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 - a row failure must not fail the run
        scored["error"] = str(exc)

    # Aggregate operational metrics from the host session (best-effort, runs
    # even when the host request errored so partial spend is still recorded).
    scored["metrics"] = await dbos_class.run_step_async(
        {"name": f"score-row.metrics.{row_id}"},
        _collect_row_metrics,
        runner_id,
        session_id,
        primary_agent_id,
    )
    return scored


class ScoreEvalsStrategy(WorkflowStrategy):
    """Durable, parallel scoring for the ``score-evals`` workflow."""

    def __init__(
        self, *, context: MasherRuntimeContext, row_concurrency: int = _DEFAULT_ROW_CONCURRENCY
    ) -> None:
        self._context = context
        self._row_concurrency = max(1, int(row_concurrency))

    def register(self, dbos_class: Any) -> None:
        if _STATE.workflow is not None:
            return
        _, queue_class, _, _, _ = load_dbos_api()
        _STATE.queue = queue_class(_ROW_QUEUE_NAME, concurrency=self._row_concurrency)
        _STATE.workflow = dbos_class.workflow(name="mash.eval.score_row")(_score_row)

    async def run(self, ctx: WorkflowExecutionContext) -> dict[str, Any]:
        dbos_class, _, set_workflow_id, _, _ = load_dbos_api()
        run_id = ctx.run_id
        eval_id = str(ctx.workflow_input.get("eval_id") or "").strip()
        if not eval_id:
            return {"status": "failed", "error": "workflow_input.eval_id is required"}

        pool = require_runner(ctx.runner_id)
        service = self._context.require_eval_service()

        # 1) Load the eval (dataset rows + rubric + host_id).
        async def _load() -> dict[str, Any]:
            return await service.get_eval_detail(eval_id)

        detail = await dbos_class.run_step_async({"name": "score.load"}, _load)
        eval_meta = detail.get("eval") or {}
        host_id = str(eval_meta.get("host_id") or "").strip()
        rows = detail.get("rows") or []
        rubric = detail.get("rubric") or {}
        if not host_id:
            return {"status": "failed", "error": "eval is missing host_id"}
        if not rubric:
            return {"status": "failed", "error": "eval has no rubric"}

        host = pool.get_host(host_id)
        host_snapshot = pool.snapshot_for(host)

        # 2) Create the experiment up front with a snapshot of the live host —
        # a record of exactly what is being evaluated. created_at marks the
        # start of scoring (completed_at is stamped at the end), giving a real
        # duration.
        async def _create() -> dict[str, Any]:
            experiment = await service.persist_experiment(
                eval_id=eval_id,
                host_composition=host_snapshot,
                agent_spec_snapshot=pool.snapshot_host_agent_specs(host_id),
            )
            return {"experiment_id": experiment.experiment_id}

        experiment_id = (
            await dbos_class.run_step_async({"name": "score.create"}, _create)
        )["experiment_id"]

        # 3) Fan out one durable child workflow per row over the row queue.
        # The first row runs alone to warm the provider prompt cache: every row
        # sends the same host-agent prompt prefix, and a provider cache entry
        # only becomes readable after the request writing it starts responding,
        # so a cold concurrent fan-out makes each in-flight row pay the full
        # cache write for the shared prefix. Serializing row 0 turns that into
        # one write plus cheap cache reads for the rest.
        scored_rows: list[dict[str, Any]] = []
        handles = []
        for index, row in enumerate(rows):
            child_id = f"{run_id}:row:{index}"
            with set_workflow_id(child_id):
                handle = await _STATE.queue.enqueue_async(
                    _STATE.workflow,
                    ctx.runner_id,
                    run_id,
                    host_id,
                    host_snapshot,
                    host.primary,
                    row,
                    rubric,
                )
            if index == 0:
                scored_rows.append(await handle.get_result())
            else:
                handles.append(handle)
        scored_rows.extend([await handle.get_result() for handle in handles])

        # 4) Persist runs and finalize the experiment (single memoized step).
        # Deterministic run_id keyed by (experiment, row) so a partial retry
        # upserts rather than duplicating.
        async def _finalize() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            weighted_values: list[float] = []
            for row in scored_rows:
                row_id = str(row.get("row_id") or "")
                scores = {
                    name: CriterionScore(
                        score=int(entry["score"]),
                        rationale=str(entry.get("rationale") or ""),
                    )
                    for name, entry in (row.get("scores") or {}).items()
                }
                weighted = row.get("weighted_score")
                if weighted is not None:
                    weighted_values.append(float(weighted))
                await service.persist_run(
                    ExperimentRun(
                        run_id=f"run:{experiment_id}:{row_id}",
                        experiment_id=experiment_id,
                        row_id=row_id,
                        input=str(row.get("input") or ""),
                        actual_output=row.get("actual_output"),
                        weighted_score=weighted,
                        scores=scores,
                        created_at=now,
                        session_id=row.get("session_id"),
                        error=row.get("error"),
                        metrics=row.get("metrics"),
                    )
                )
            mean_score = (
                sum(weighted_values) / len(weighted_values) if weighted_values else None
            )
            status = "completed" if weighted_values else "failed"
            await service.update_experiment_status(
                experiment_id,
                status,
                completed_at=now,
            )
            return {
                "experiment_id": experiment_id,
                "eval_id": eval_id,
                "status": status,
                "scored_count": len(weighted_values),
                "mean_score": mean_score,
            }

        return await dbos_class.run_step_async({"name": "score.persist"}, _finalize)

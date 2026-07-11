"""Masher's bundled workflow definitions.

Four workflows, sorted along the judgment/computation line:

- ``masher-trace-digest`` — all code. Deterministic latency analysis over
  runtime traces; no model inference anywhere.
- ``masher-online-eval-curation`` — all code. Mechanical extraction of eval
  rows from runtime traces.
- ``gen-synthetic-evals`` — code, agent, code. Deterministic host profiling,
  one agent-loop generation step (dataset rows + rubric as structured output),
  deterministic persistence into the eval store.
- ``run-experiment`` — code-driven host execution and judging fan-outs backed
  by the durable experiment-row ledger.

Code steps close over a :class:`MasherRuntimeContext`; its dependencies are
bound by ``HostBuilder.build()`` (pool) and API startup (eval service), before
any run executes.

Batch scans take an optional ``since_ts`` watermark in ``workflow_input`` and
report ``latest_event_at`` in the run result — the caller persists it and
passes it back as the next run's ``since_ts`` (cross-run state lives at the
trigger boundary, never inside the workflow).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ...workflows import AgentStep, CodeStep, StepContext, WorkflowSpec
from ...workflows.dbos import (
    collect_terminal_payload,
    post_inline_agent_request,
)
from ...evals.metrics import METRIC_EVENT_TYPES, compute_row_metrics
from .context import MasherRuntimeContext
from .judge import EVAL_JUDGE_STRUCTURED_OUTPUT, build_judge_message, parse_judge_output
from .spec import EVAL_JUDGE_AGENT_ID
from .traces import (
    append_jsonl_unique,
    build_online_eval_row,
    build_trace_digest,
    list_traces_since,
    load_trace_events,
    stitch_subagent_traces,
)
from ...runtime.events import analyze_trace, build_runtime_trace, build_span_tree
from ...runtime.spec import AgentSpec

MASHER_TRACE_DIGEST_WORKFLOW_ID = "masher-trace-digest"
MASHER_ONLINE_EVAL_WORKFLOW_ID = "masher-online-eval-curation"
MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID = "gen-synthetic-evals"
MASHER_RUN_EXPERIMENT_WORKFLOW_ID = "run-experiment"
GEN_SYNTHETIC_EVALS_SKILL_NAME = "gen-synthetic-evals"

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- Trace scanning (shared by the two all-code workflows) --------------------


class TraceScanInput(BaseModel):
    """``workflow_input`` for both trace-scanning workflows.

    ``trace`` mode analyzes exactly one trace; ``batch`` mode scans all of the
    target's traces newer than ``since_ts`` (capped at ``limit``).
    """

    mode: Literal["trace", "batch"] = "batch"
    target_agent_id: _Text
    session_id: str | None = None
    trace_id: str | None = None
    since_ts: float = 0.0
    limit: int = Field(default=100, ge=1)

    @model_validator(mode="after")
    def _require_trace_coordinates(self) -> "TraceScanInput":
        if self.mode == "trace" and not (
            (self.session_id or "").strip() and (self.trace_id or "").strip()
        ):
            raise ValueError("mode 'trace' requires session_id and trace_id")
        return self


class TraceRef(BaseModel):
    session_id: str
    trace_id: str


class TraceListing(BaseModel):
    traces: list[TraceRef]
    latest_event_at: float | None = None


def _build_list_traces(context: MasherRuntimeContext):
    async def list_traces(inp: TraceScanInput, _ctx: StepContext) -> TraceListing:
        if inp.mode == "trace":
            # The input validator guarantees both coordinates in trace mode.
            return TraceListing(
                traces=[
                    TraceRef(
                        session_id=inp.session_id or "",
                        trace_id=inp.trace_id or "",
                    )
                ]
            )
        store = context.require_runtime_store()
        traces = await list_traces_since(
            store,
            target_agent_id=inp.target_agent_id,
            since_ts=inp.since_ts,
            limit=inp.limit,
        )
        return TraceListing(
            traces=[
                TraceRef(session_id=item["session_id"], trace_id=item["trace_id"])
                for item in traces
            ],
            latest_event_at=max(
                (float(item["latest_event_at"]) for item in traces), default=None
            ),
        )

    return list_traces


# --- masher-trace-digest -------------------------------------------------------


class _AnalyzeTracesIn(BaseModel):
    mode: Literal["trace", "batch"] = "batch"
    target_agent_id: str
    traces: list[TraceRef]
    latest_event_at: float | None = None


class DigestBatch(BaseModel):
    digests: list[dict[str, Any]]
    latest_event_at: float | None = None


class _AppendDigestsIn(BaseModel):
    mode: Literal["trace", "batch"] = "batch"
    digests: list[dict[str, Any]]
    latest_event_at: float | None = None


class TraceDigestResult(BaseModel):
    schema_version: int = 3
    mode: str
    processed_trace_count: int
    appended_trace_count: int = 0
    artifact_path: str | None = None
    digest: dict[str, Any] | None = None
    latest_event_at: float | None = None


def _build_digest_traces(context: MasherRuntimeContext):
    async def digest_traces(inp: _AnalyzeTracesIn, _ctx: StepContext) -> DigestBatch:
        store = context.require_runtime_store()
        digests: list[dict[str, Any]] = []
        for ref in inp.traces:
            try:
                events = await load_trace_events(
                    store,
                    target_agent_id=inp.target_agent_id,
                    session_id=ref.session_id,
                    trace_id=ref.trace_id,
                )
            except RuntimeError:
                if inp.mode == "trace":
                    raise
                continue
            bundle = build_runtime_trace(events)
            analysis = analyze_trace(build_span_tree(events))
            analysis = await stitch_subagent_traces(store, analysis)
            digests.append(build_trace_digest(bundle, analysis))
        return DigestBatch(digests=digests, latest_event_at=inp.latest_event_at)

    return digest_traces


def _build_append_digests(context: MasherRuntimeContext):
    def append_digests(inp: _AppendDigestsIn, _ctx: StepContext) -> TraceDigestResult:
        # Trace mode returns the digest inline and never writes the artifact,
        # matching the trace-mode contract.
        if inp.mode == "trace":
            return TraceDigestResult(
                mode="trace",
                processed_trace_count=len(inp.digests),
                digest=inp.digests[0] if inp.digests else None,
            )
        path = context.require_trace_digest_jsonl_path()
        appended = sum(1 for digest in inp.digests if append_jsonl_unique(path, digest))
        return TraceDigestResult(
            mode="batch",
            processed_trace_count=len(inp.digests),
            appended_trace_count=appended,
            artifact_path=str(path),
            latest_event_at=inp.latest_event_at,
        )

    return append_digests


def build_trace_digest_workflow(context: MasherRuntimeContext) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=MASHER_TRACE_DIGEST_WORKFLOW_ID,
        input_model=TraceScanInput,
        steps=[
            CodeStep(
                step_id="list-traces",
                run=_build_list_traces(context),
                input=TraceScanInput,
                output=TraceListing,
            ),
            CodeStep(
                step_id="digest-traces",
                run=_build_digest_traces(context),
                input=_AnalyzeTracesIn,
                output=DigestBatch,
            ),
            CodeStep(
                step_id="append-digests",
                run=_build_append_digests(context),
                input=_AppendDigestsIn,
                output=TraceDigestResult,
            ),
        ],
    )


# --- masher-online-eval-curation ------------------------------------------------


class EvalRowBatch(BaseModel):
    rows: list[dict[str, Any]]
    latest_event_at: float | None = None


class _AppendRowsIn(BaseModel):
    mode: Literal["trace", "batch"] = "batch"
    rows: list[dict[str, Any]]
    latest_event_at: float | None = None


class OnlineEvalResult(BaseModel):
    schema_version: int = 3
    mode: str
    processed_trace_count: int
    appended_trace_count: int
    artifact_path: str
    record: dict[str, Any] | None = None
    latest_event_at: float | None = None


def _build_extract_rows(context: MasherRuntimeContext):
    async def extract_rows(inp: _AnalyzeTracesIn, _ctx: StepContext) -> EvalRowBatch:
        store = context.require_runtime_store()
        rows: list[dict[str, Any]] = []
        for ref in inp.traces:
            try:
                events = await load_trace_events(
                    store,
                    target_agent_id=inp.target_agent_id,
                    session_id=ref.session_id,
                    trace_id=ref.trace_id,
                )
            except RuntimeError:
                if inp.mode == "trace":
                    raise
                continue
            bundle = build_runtime_trace(events)
            analysis = analyze_trace(build_span_tree(events))
            rows.append(build_online_eval_row(bundle, analysis))
        return EvalRowBatch(rows=rows, latest_event_at=inp.latest_event_at)

    return extract_rows


def _build_append_rows(context: MasherRuntimeContext):
    def append_rows(inp: _AppendRowsIn, _ctx: StepContext) -> OnlineEvalResult:
        path = context.require_online_eval_jsonl_path()
        appended = sum(1 for row in inp.rows if append_jsonl_unique(path, row))
        return OnlineEvalResult(
            mode=inp.mode,
            processed_trace_count=len(inp.rows),
            appended_trace_count=appended,
            artifact_path=str(path),
            record=inp.rows[0] if inp.mode == "trace" and inp.rows else None,
            latest_event_at=inp.latest_event_at,
        )

    return append_rows


def build_online_eval_curation_workflow(context: MasherRuntimeContext) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=MASHER_ONLINE_EVAL_WORKFLOW_ID,
        input_model=TraceScanInput,
        steps=[
            CodeStep(
                step_id="list-traces",
                run=_build_list_traces(context),
                input=TraceScanInput,
                output=TraceListing,
            ),
            CodeStep(
                step_id="extract-rows",
                run=_build_extract_rows(context),
                input=_AnalyzeTracesIn,
                output=EvalRowBatch,
            ),
            CodeStep(
                step_id="append-rows",
                run=_build_append_rows(context),
                input=_AppendRowsIn,
                output=OnlineEvalResult,
            ),
        ],
    )


# --- gen-synthetic-evals ---------------------------------------------------------


class GenSyntheticEvalsInput(BaseModel):
    host_id: _Text
    user_guidance: str = ""
    row_count: int = Field(default=20, ge=1, le=100)


class AgentProfile(BaseModel):
    agent_id: str
    role: Literal["primary", "subagent"]
    display_name: str = ""
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    usage_guidance: str = ""


class HostProfile(BaseModel):
    primary_agent_id: str
    agent_profiles: list[AgentProfile]


class GenerationBrief(BaseModel):
    """Input to the generation agent step: the trigger params plus the host's
    actual composition and declared capabilities."""

    host_id: str
    user_guidance: str = ""
    row_count: int = Field(default=20, ge=1, le=100)
    primary_agent_id: str
    agent_profiles: list[AgentProfile]


class DatasetRow(BaseModel):
    input: _Text
    scenario_description: _Text
    expected_behavior: _Text
    sampling_category: Literal[
        "random",
        "multi_tool",
        "multi_agent",
        "high_tokens",
        "long_running",
        "short_running",
    ]
    target_agents: list[str] = Field(default_factory=list)


class RubricCriterion(BaseModel):
    name: _Text
    description: _Text
    scoring_prompt: _Text
    weight: float = Field(ge=0.0, le=1.0)
    scale_min: int = 1
    scale_max: int = 5


_WEIGHT_SUM_TOLERANCE = 1e-6


class Rubric(BaseModel):
    global_scoring_prompt: str = ""
    criteria: list[RubricCriterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "Rubric":
        weight_sum = sum(criterion.weight for criterion in self.criteria)
        if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"rubric criteria weights must sum to 1.0 (got {weight_sum})"
            )
        return self


class GeneratedEval(BaseModel):
    dataset_rows: list[DatasetRow] = Field(min_length=1, max_length=100)
    rubric: Rubric


class _PersistEvalIn(BaseModel):
    host_id: str
    user_guidance: str = ""
    row_count: int = Field(default=20, ge=1, le=100)
    dataset_rows: list[DatasetRow]
    rubric: Rubric


class GenSyntheticEvalsResult(BaseModel):
    eval_id: str
    host_id: str
    dataset_id: str
    rubric_id: str
    row_count: int


def _build_profile_host(context: MasherRuntimeContext):
    def profile_host(inp: GenSyntheticEvalsInput, _ctx: StepContext) -> HostProfile:
        pool = context.require_pool()
        host = pool.get_host(inp.host_id)
        members: list[tuple[Literal["primary", "subagent"], str]] = [
            ("primary", host.primary),
            *(("subagent", agent_id) for agent_id in host.subagents),
        ]
        profiles: list[AgentProfile] = []
        for role, agent_id in members:
            metadata = pool.get_agent_metadata(agent_id)
            profiles.append(
                AgentProfile(
                    agent_id=agent_id,
                    role=role,
                    display_name=metadata.display_name if metadata else "",
                    description=metadata.description if metadata else "",
                    capabilities=list(metadata.capabilities) if metadata else [],
                    usage_guidance=metadata.usage_guidance if metadata else "",
                )
            )
        return HostProfile(primary_agent_id=host.primary, agent_profiles=profiles)

    return profile_host


def _build_persist_eval(context: MasherRuntimeContext):
    async def persist_eval(inp: _PersistEvalIn, _ctx: StepContext) -> GenSyntheticEvalsResult:
        # The generation step's output is memoized, so a failed count here is
        # terminal for this run (resume replays the same rows) — start a fresh
        # run to regenerate.
        if len(inp.dataset_rows) != inp.row_count:
            raise ValueError(
                f"generation produced {len(inp.dataset_rows)} dataset rows; "
                f"workflow_input.row_count requires exactly {inp.row_count}"
            )
        service = context.require_eval_service()
        eval_ = await service.persist_eval(
            host_id=inp.host_id,
            user_guidance=inp.user_guidance,
            dataset_rows=[row.model_dump() for row in inp.dataset_rows],
            rubric=inp.rubric.model_dump(),
        )
        return GenSyntheticEvalsResult(
            eval_id=eval_.eval_id,
            host_id=eval_.host_id,
            dataset_id=eval_.dataset_id,
            rubric_id=eval_.rubric_id,
            row_count=len(inp.dataset_rows),
        )

    return persist_eval


def build_gen_synthetic_evals_workflow(eval_agent_spec: AgentSpec) -> WorkflowSpec:
    context: MasherRuntimeContext = eval_agent_spec.runtime_context  # type: ignore[attr-defined]
    return WorkflowSpec(
        workflow_id=MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID,
        input_model=GenSyntheticEvalsInput,
        steps=[
            CodeStep(
                step_id="profile-host",
                run=_build_profile_host(context),
                input=GenSyntheticEvalsInput,
                output=HostProfile,
            ),
            AgentStep(
                step_id="generate",
                agent_spec=eval_agent_spec,
                input=GenerationBrief,
                output=GeneratedEval,
                skill_name=GEN_SYNTHETIC_EVALS_SKILL_NAME,
            ),
            CodeStep(
                step_id="persist-eval",
                run=_build_persist_eval(context),
                input=_PersistEvalIn,
                output=GenSyntheticEvalsResult,
            ),
        ],
    )


# --- run-experiment ----------------------------------------------------------


class RunExperimentInput(BaseModel):
    eval_id: _Text
    host_id: _Text


class ExperimentRef(BaseModel):
    experiment_id: str


class RunExperimentResult(BaseModel):
    experiment_id: str
    eval_id: str
    host_id: str
    status: Literal["completed", "failed"]
    row_count: int
    scored_count: int
    failed_count: int
    mean_score: float | None = None


def _experiment_id(run_id: str) -> str:
    return f"exp_{uuid.uuid5(uuid.NAMESPACE_URL, f'mash.eval.experiment:{run_id}').hex}"


def _response_text(payload: dict[str, Any]) -> str | None:
    response = payload.get("response")
    text = response.get("text") if isinstance(response, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def _response_json_text(payload: dict[str, Any]) -> str:
    response = payload.get("response")
    structured = response.get("structured_output") if isinstance(response, dict) else None
    if not isinstance(structured, dict) or not isinstance(structured.get("json_text"), str):
        raise ValueError("judge response did not return json_text")
    return structured["json_text"]


def _error_text(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


async def _fan_out_after_warmup(
    rows: list[Any], start: Any, finish: Any, concurrency: int = 8
) -> None:
    """Warm one row, then start deterministic batches and await each concurrently.

    Starting child workflows mutates the parent DBOS workflow's function
    sequence, so starts remain serial and deterministic. Once a batch is
    started, terminal collection and row persistence can run concurrently.
    """
    if not rows:
        return
    first = await start(rows[0])
    if first is not None:
        await finish(*first)

    width = max(1, int(concurrency))
    for offset in range(1, len(rows), width):
        started = []
        for row in rows[offset : offset + width]:
            operation = await start(row)
            if operation is not None:
                started.append(operation)
        await asyncio.gather(*(finish(*operation) for operation in started))


async def _collect_experiment_metrics(
    context: MasherRuntimeContext,
    *,
    session_id: str,
    primary_agent_id: str,
) -> dict[str, Any] | None:
    try:
        store = context.require_runtime_store()
        events = await store.list_session_events(
            session_id, event_types=list(METRIC_EVENT_TYPES)
        )
        return compute_row_metrics(
            events, primary_agent_id=primary_agent_id
        ).to_dict()
    except Exception:  # noqa: BLE001 - metrics never fail experiment work
        return None


def _build_prepare_experiment(context: MasherRuntimeContext):
    async def prepare_experiment(
        inp: RunExperimentInput, ctx: StepContext
    ) -> ExperimentRef:
        service = context.require_eval_service()
        pool = context.require_pool()
        detail = await service.get_eval_detail(inp.eval_id)
        eval_meta = detail.get("eval") or {}
        source_host_id = str(eval_meta.get("host_id") or "").strip()
        if source_host_id != inp.host_id:
            raise ValueError(
                f"eval '{inp.eval_id}' belongs to host '{source_host_id}', not '{inp.host_id}'"
            )
        rows = list(detail.get("rows") or [])
        rubric = detail.get("rubric") or {}
        if not rows:
            raise ValueError(f"eval '{inp.eval_id}' has no dataset rows")
        if not rubric:
            raise ValueError(f"eval '{inp.eval_id}' has no rubric")
        host = pool.get_host(inp.host_id)
        experiment_id = _experiment_id(ctx.run_id)
        await service.prepare_experiment(
            experiment_id=experiment_id,
            workflow_run_id=ctx.run_id,
            eval_id=inp.eval_id,
            target_host_id=inp.host_id,
            host_composition=pool.snapshot_for(host),
            agent_spec_snapshot=pool.snapshot_host_agent_specs(inp.host_id),
            rubric_snapshot=rubric,
            rows=rows,
        )
        return ExperimentRef(experiment_id=experiment_id)

    return prepare_experiment


def _build_execute_experiment_rows(context: MasherRuntimeContext):
    async def execute_rows(inp: ExperimentRef, ctx: StepContext) -> ExperimentRef:
        service = context.require_eval_service()
        pool = context.require_pool()
        experiment = await service.get_experiment(inp.experiment_id)
        host_snapshot = dict(experiment.host_composition)
        primary_agent_id = str(host_snapshot.get("primary") or "").strip()
        if not primary_agent_id:
            raise ValueError("experiment host snapshot has no primary agent")
        rows = await service.list_runs(inp.experiment_id, limit=1000)
        pending = [row for row in rows if row.status in {"pending", "executing"}]

        async def finish(
            active: Any,
            request_id: str | None,
            initial_error: str | None = None,
        ) -> None:
            error = initial_error
            actual_output: str | None = None
            if request_id is not None:
                try:
                    payload = await collect_terminal_payload(
                        pool.runner_id, primary_agent_id, request_id
                    )
                    actual_output = _response_text(payload)
                except Exception as exc:  # noqa: BLE001 - row failure
                    error = _error_text(exc)
            metrics = await _collect_experiment_metrics(
                context,
                session_id=active.session_id
                or f"eval:{ctx.run_id}:{active.row_id}",
                primary_agent_id=primary_agent_id,
            )
            await service.persist_run(
                replace(
                    active,
                    status=(
                        "execution_failed" if error is not None else "executed"
                    ),
                    actual_output=actual_output,
                    error=error,
                    metrics=metrics,
                    updated_at=datetime.now(timezone.utc),
                )
            )

        async def start(row: Any) -> tuple[Any, str] | None:
            now = datetime.now(timezone.utc)
            active = replace(row, status="executing", error=None, updated_at=now)
            await service.persist_run(active)
            try:
                request_id = await post_inline_agent_request(
                    pool.runner_id,
                    agent_id=primary_agent_id,
                    message=row.input,
                    structured_output=None,
                    workflow_id=MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                    workflow_run_id=ctx.run_id,
                    task_id=f"execute:{row.row_id}",
                    session_id=row.session_id or f"eval:{ctx.run_id}:{row.row_id}",
                    host_snapshot=host_snapshot,
                )
            except Exception as exc:  # noqa: BLE001 - row failures are isolated
                await finish(active, None, _error_text(exc))
                return None
            return active, request_id

        await _fan_out_after_warmup(pending, start, finish)
        return inp

    return execute_rows


def _build_judge_experiment_rows(context: MasherRuntimeContext):
    async def judge_rows(inp: ExperimentRef, ctx: StepContext) -> RunExperimentResult:
        service = context.require_eval_service()
        pool = context.require_pool()
        experiment = await service.get_experiment(inp.experiment_id)
        rubric = dict(experiment.rubric_snapshot or {})
        if not rubric:
            raise ValueError("experiment has no rubric snapshot")
        rows = await service.list_runs(inp.experiment_id, limit=1000)
        pending = [row for row in rows if row.status in {"executed", "judging"}]

        async def finish(
            active: Any,
            request_id: str | None,
            initial_error: str | None = None,
        ) -> None:
            if request_id is None:
                updated = replace(
                    active,
                    status="scoring_failed",
                    error=initial_error or "judge request failed",
                    updated_at=datetime.now(timezone.utc),
                )
            else:
                try:
                    payload = await collect_terminal_payload(
                        pool.runner_id, EVAL_JUDGE_AGENT_ID, request_id
                    )
                    scores, weighted_score = parse_judge_output(
                        _response_json_text(payload), rubric
                    )
                    updated = replace(
                        active,
                        status="scored",
                        scores=scores,
                        weighted_score=weighted_score,
                        updated_at=datetime.now(timezone.utc),
                    )
                except Exception as exc:  # noqa: BLE001 - row failure
                    updated = replace(
                        active,
                        status="scoring_failed",
                        error=_error_text(exc),
                        updated_at=datetime.now(timezone.utc),
                    )
            await service.persist_run(updated)

        async def start(row: Any) -> tuple[Any, str] | None:
            active = replace(
                row,
                status="judging",
                error=None,
                updated_at=datetime.now(timezone.utc),
            )
            await service.persist_run(active)
            try:
                request_id = await post_inline_agent_request(
                    pool.runner_id,
                    agent_id=EVAL_JUDGE_AGENT_ID,
                    message=build_judge_message(
                        row_input=row.input,
                        actual_output=row.actual_output,
                        rubric=rubric,
                    ),
                    structured_output=EVAL_JUDGE_STRUCTURED_OUTPUT,
                    workflow_id=MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
                    workflow_run_id=ctx.run_id,
                    task_id=f"judge:{row.row_id}",
                    session_id=row.session_id or f"eval:{ctx.run_id}:{row.row_id}",
                )
            except Exception as exc:  # noqa: BLE001 - row failures are isolated
                await finish(active, None, _error_text(exc))
                return None
            return active, request_id

        await _fan_out_after_warmup(pending, start, finish)

        final_rows = await service.list_runs(inp.experiment_id, limit=1000)
        scored = [row for row in final_rows if row.status == "scored"]
        failed = [
            row
            for row in final_rows
            if row.status in {"execution_failed", "scoring_failed"}
        ]
        values = [float(row.weighted_score) for row in scored if row.weighted_score is not None]
        status: Literal["completed", "failed"] = "completed" if values else "failed"
        await service.update_experiment_status(
            inp.experiment_id,
            status,
            completed_at=datetime.now(timezone.utc),
        )
        return RunExperimentResult(
            experiment_id=inp.experiment_id,
            eval_id=experiment.eval_id,
            host_id=experiment.target_host_id or "",
            status=status,
            row_count=len(final_rows),
            scored_count=len(scored),
            failed_count=len(failed),
            mean_score=sum(values) / len(values) if values else None,
        )

    return judge_rows


def build_run_experiment_workflow(context: MasherRuntimeContext) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=MASHER_RUN_EXPERIMENT_WORKFLOW_ID,
        input_model=RunExperimentInput,
        steps=[
            CodeStep(
                step_id="prepare-experiment",
                run=_build_prepare_experiment(context),
                input=RunExperimentInput,
                output=ExperimentRef,
            ),
            CodeStep(
                step_id="execute-rows",
                run=_build_execute_experiment_rows(context),
                input=ExperimentRef,
                output=ExperimentRef,
                orchestration=True,
            ),
            CodeStep(
                step_id="judge-rows",
                run=_build_judge_experiment_rows(context),
                input=ExperimentRef,
                output=RunExperimentResult,
                agent_ids=[EVAL_JUDGE_AGENT_ID],
                orchestration=True,
            ),
        ],
    )


def build_masher_workflows(eval_agent_spec: AgentSpec) -> list[WorkflowSpec]:
    """Build the complete workflow set attached to every host."""
    context: MasherRuntimeContext = eval_agent_spec.runtime_context  # type: ignore[attr-defined]
    return [
        build_trace_digest_workflow(context),
        build_online_eval_curation_workflow(context),
        build_gen_synthetic_evals_workflow(eval_agent_spec),
        build_run_experiment_workflow(context),
    ]


__all__ = [
    "GEN_SYNTHETIC_EVALS_SKILL_NAME",
    "AgentProfile",
    "DatasetRow",
    "DigestBatch",
    "EvalRowBatch",
    "GenSyntheticEvalsInput",
    "GenSyntheticEvalsResult",
    "GeneratedEval",
    "GenerationBrief",
    "HostProfile",
    "MASHER_GEN_SYNTHETIC_EVALS_WORKFLOW_ID",
    "MASHER_ONLINE_EVAL_WORKFLOW_ID",
    "MASHER_RUN_EXPERIMENT_WORKFLOW_ID",
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "OnlineEvalResult",
    "Rubric",
    "RubricCriterion",
    "RunExperimentInput",
    "RunExperimentResult",
    "TraceDigestResult",
    "TraceListing",
    "TraceRef",
    "TraceScanInput",
    "build_gen_synthetic_evals_workflow",
    "build_masher_workflows",
    "build_online_eval_curation_workflow",
    "build_run_experiment_workflow",
    "build_trace_digest_workflow",
]

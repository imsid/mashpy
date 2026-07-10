"""Masher's v2 step pipelines.

Three workflows, sorted along the judgment/computation line:

- ``masher-trace-digest`` — all code. Deterministic latency analysis over
  runtime traces; no model inference anywhere.
- ``masher-online-eval-curation`` — all code. Mechanical extraction of eval
  rows from runtime traces.
- ``gen-synthetic-evals`` — code, agent, code. Deterministic host profiling,
  one agent-loop generation step (dataset rows + rubric as structured output),
  deterministic persistence into the eval store.

Code steps close over a :class:`MasherRuntimeContext`; its dependencies are
bound by ``HostBuilder.build()`` (pool) and API startup (eval service), before
any run executes.

Batch scans take an optional ``since_ts`` watermark in ``workflow_input`` and
report ``latest_event_at`` in the run result — the caller persists it and
passes it back as the next run's ``since_ts`` (cross-run state lives at the
trigger boundary, never inside the workflow).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ...workflows import AgentStep, CodeStep, StepContext, WorkflowSpec
from .context import MasherRuntimeContext
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
        # matching the pre-v2 contract.
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


def build_gen_synthetic_evals_workflow(masher_spec: AgentSpec) -> WorkflowSpec:
    context: MasherRuntimeContext = masher_spec.runtime_context  # type: ignore[attr-defined]
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
                agent_spec=masher_spec,
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
    "MASHER_TRACE_DIGEST_WORKFLOW_ID",
    "OnlineEvalResult",
    "Rubric",
    "RubricCriterion",
    "TraceDigestResult",
    "TraceListing",
    "TraceRef",
    "TraceScanInput",
    "build_gen_synthetic_evals_workflow",
    "build_online_eval_curation_workflow",
    "build_trace_digest_workflow",
]

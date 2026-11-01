"""Project a workflow run onto a trace of its own.

A run is a root span ``mash.workflow {workflow_id}`` with one span per step
from ``workflow_steps``. An ``AgentStep`` span is the parent of the request it
submitted, found through ``agent_request_id``; that request's spans come from
the request projection, so they read the same as anywhere else.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..runtime.events.types import RuntimeEvent
from . import semconv
from .ids import span_id_hex, trace_id_hex
from .loading import load_request_tree
from .projection import (
    KIND_INTERNAL,
    STATUS_ERROR,
    Projection,
    Span,
    default_resource,
    project_request,
)

_FAILED = "failed"
_CANCELLED = "cancelled"


def project_workflow_run(
    run: Any,
    steps: list[Any],
    requests: Mapping[str, tuple[list[RuntimeEvent], dict[str, list[RuntimeEvent]]]],
    *,
    capture_content: bool | None = None,
    resource: Mapping[str, Any] | None = None,
) -> Projection:
    """``run`` and ``steps`` are workflow store records; ``requests`` maps an
    agent step's request id to its events and its children's."""
    trace_hex = trace_id_hex(run.run_id)
    start = float(run.started_at or run.created_at or 0.0)
    root = Span(
        trace_id=trace_hex,
        span_id=span_id_hex(run.run_id, "root"),
        parent_span_id=None,
        key="root",
        name=f"mash.workflow {run.workflow_id}",
        kind=KIND_INTERNAL,
        start_time=start,
        end_time=float(run.finished_at) if run.finished_at is not None else None,
        attributes={
            semconv.MASH_WORKFLOW_ID: run.workflow_id,
            semconv.MASH_WORKFLOW_RUN_ID: run.run_id,
            semconv.MASH_WORKFLOW_STATUS: run.status,
        },
    )
    if run.status == _FAILED:
        root.status = STATUS_ERROR
        root.status_message = run.error
        root.attributes[semconv.ERROR_TYPE] = "workflow_failed"
    spans: list[Span] = [root]
    logs = []
    observed_at = root.end_time or start
    for step in sorted(steps, key=lambda s: s.ordinal):
        step_start = float(step.started_at or start)
        span = Span(
            trace_id=trace_hex,
            span_id=span_id_hex(run.run_id, f"step.{step.step_id}"),
            parent_span_id=root.span_id,
            key=f"step.{step.step_id}",
            name=f"mash.workflow.step {step.step_id}",
            kind=KIND_INTERNAL,
            start_time=step_start,
            end_time=float(step.finished_at) if step.finished_at is not None else None,
            attributes={
                semconv.MASH_WORKFLOW_STEP_ID: step.step_id,
                semconv.MASH_WORKFLOW_STEP_KIND: step.kind,
                semconv.MASH_WORKFLOW_STEP_ATTEMPT: int(step.attempt or 1),
            },
        )
        if step.status == _FAILED:
            span.status = STATUS_ERROR
            span.status_message = step.error
            span.attributes[semconv.ERROR_TYPE] = "step_failed"
        if span.end_time is None and root.end_time is not None:
            span.end_time = root.end_time
        spans.append(span)
        observed_at = max(observed_at, span.end_time or step_start)
        request = requests.get(step.agent_request_id or "")
        if request is not None:
            events, children = request
            nested = project_request(
                events,
                children=children,
                capture_content=capture_content,
                parent=(trace_hex, span.span_id),
            )
            spans.extend(nested.spans)
            logs.extend(nested.logs)
            observed_at = max(observed_at, nested.observed_at)
    return Projection(
        trace_id=trace_hex,
        root_span_id=root.span_id,
        spans=tuple(sorted(spans, key=lambda s: (s.start_time, s.span_id))),
        logs=tuple(sorted(logs, key=lambda r: r.timestamp)),
        resource=dict(resource) if resource is not None else default_resource(),
        observed_at=observed_at,
    )


async def load_workflow_projection(
    workflow_store: Any, runtime_store: Any, finished: Mapping[str, Any]
) -> Projection | None:
    """The projection of one finished run from ``list_finished_runs``."""
    run = finished["run"]
    steps = await workflow_store.get_run_steps(run.run_id)
    requests: dict[str, tuple[list[RuntimeEvent], dict[str, list[RuntimeEvent]]]] = {}
    for step in steps:
        if step.agent_request_id and step.agent_request_id not in requests:
            requests[step.agent_request_id] = await load_request_tree(
                runtime_store, step.agent_request_id
            )
    return project_workflow_run(run, steps, requests)

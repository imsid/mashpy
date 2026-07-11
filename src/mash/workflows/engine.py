"""Forward-pipeline workflow engine.

Runs a ``WorkflowSpec.steps`` pipeline: step *n*'s output threads into step
*n+1*'s input (merged over the immutable ``workflow_input``), coerced and
validated through pydantic models at each boundary. The final step's output is
the run result.

Durability and observability come from DBOS plus the workflow store:

- each step body and each store write is its own ``run_step_async`` step, so DBOS
  memoizes it and skips it on replay;
- store writes are idempotent (``workflow_steps`` upsert keyed by
  ``(run_id, step_id)``; ``workflow_step_events`` insert keyed by
  ``(run_id, step_id, attempt, event_type)``), so the rare crash-after-effect
  re-run converges instead of duplicating.

Agent steps interlock with their own durable request workflow through a
deterministic ``request_id`` (see ``post_inline_agent_request``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from mash.runtime.structured_output import serialize_structured_output

from .dbos import (
    _run_session_id,
    collect_terminal_payload,
    load_dbos_api,
    post_inline_agent_request,
    require_runner,
)
from .spec import CodeStep, StepContext, StepSpec, WorkflowSpec
from .store import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    STEP_COMPLETED,
    STEP_EVENT_COMPLETED,
    STEP_EVENT_FAILED,
    STEP_EVENT_STARTED,
    STEP_FAILED,
    STEP_RUNNING,
    WorkflowRunRecord,
    WorkflowStepRecord,
    WorkflowStore,
)
from .strategy import WorkflowExecutionContext, WorkflowStrategy

# Per-step attempt is fixed at 1 for now. DBOS recovery attempts are not yet
# surfaced into the step body; when they are, this becomes the recovery count so
# retried steps get distinct audit rows.
_ATTEMPT = 1


def _store(runner_id: str) -> WorkflowStore:
    pool = require_runner(runner_id)
    store = getattr(pool, "get_workflow_store", lambda: None)()
    if store is None:
        raise RuntimeError("workflow store is not available on this pool")
    return store


def _resolve_step(runner_id: str, workflow_id: str, ordinal: int) -> StepSpec:
    workflow = require_runner(runner_id).get_workflow_registry().get(workflow_id)
    return workflow.steps[ordinal]


async def _with_timeout(awaitable: Any, timeout_s: float | None, step_id: str) -> Any:
    if timeout_s is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"workflow step '{step_id}' exceeded its {timeout_s}s timeout"
        ) from exc


# --- DBOS step bodies (serializable args only) -------------------------------


async def _mark_run_started(
    runner_id: str,
    run_id: str,
    workflow_id: str,
    workflow_input: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    store = _store(runner_id)
    now = time.time()
    await store.create_run(
        WorkflowRunRecord(
            run_id=run_id,
            workflow_id=workflow_id,
            status=RUN_RUNNING,
            workflow_input=workflow_input,
            session_id=session_id,
            created_at=now,
            started_at=now,
        )
    )
    await store.mark_run_started(run_id, now)
    return {"ok": True}


async def _record_step(
    runner_id: str,
    run_id: str,
    workflow_id: str,
    step_id: str,
    ordinal: int,
    kind: str,
    status: str,
    input_snapshot: dict[str, Any] | None,
    output_snapshot: dict[str, Any] | None,
    error: str | None,
    agent_request_id: str | None,
    event_type: str,
    event_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    store = _store(runner_id)
    now = time.time()
    await store.upsert_step(
        WorkflowStepRecord(
            run_id=run_id,
            workflow_id=workflow_id,
            step_id=step_id,
            ordinal=ordinal,
            kind=kind,
            status=status,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            error=error,
            attempt=_ATTEMPT,
            agent_request_id=agent_request_id,
            started_at=now if status == STEP_RUNNING else None,
            finished_at=now if status in (STEP_COMPLETED, STEP_FAILED) else None,
        )
    )
    await store.append_step_event(
        run_id=run_id,
        workflow_id=workflow_id,
        step_id=step_id,
        event_type=event_type,
        at=now,
        attempt=_ATTEMPT,
        payload=event_payload,
    )
    return {"ok": True}


async def _finish_run(
    runner_id: str,
    run_id: str,
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    await _store(runner_id).finish_run(
        run_id, status=status, result=result, error=error, finished_at=time.time()
    )
    return {"ok": True}


async def _run_code_step(
    runner_id: str,
    run_id: str,
    workflow_id: str,
    ordinal: int,
    workflow_input: dict[str, Any],
    input_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return await _invoke_code_step(
        runner_id,
        run_id,
        workflow_id,
        ordinal,
        workflow_input,
        input_snapshot,
    )


async def _invoke_code_step(
    runner_id: str,
    run_id: str,
    workflow_id: str,
    ordinal: int,
    workflow_input: dict[str, Any],
    input_snapshot: dict[str, Any],
) -> dict[str, Any]:
    step = _resolve_step(runner_id, workflow_id, ordinal)
    if not isinstance(step, CodeStep):
        raise RuntimeError(f"workflow step '{step.step_id}' is not a code step")
    inp = step.input.model_validate(input_snapshot)
    ctx = StepContext(
        run_id=run_id,
        step_id=step.step_id,
        workflow_input=dict(workflow_input),
        attempt=_ATTEMPT,
    )

    async def _invoke() -> Any:
        result = step.run(inp, ctx)
        return await result if inspect.isawaitable(result) else result

    out = await _with_timeout(_invoke(), step.timeout_s, step.step_id)
    if not isinstance(out, step.output):
        out = step.output.model_validate(out)
    return out.model_dump(mode="json")


def _coerce_input(step: StepSpec, merged: dict[str, Any]) -> dict[str, Any]:
    """Build the step's input snapshot.

    A pydantic ``input`` model coerces and validates the merged dict; a
    passthrough (``None``) input forwards it unchanged (agent steps that
    read ``workflow_input`` directly).
    """
    if step.input is None:
        return dict(merged)
    return step.input.model_validate(merged).model_dump(mode="json")


def _extract_agent_output(step: StepSpec, payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise RuntimeError(
            f"workflow step '{step.step_id}' completed without a response payload"
        )
    structured = response.get("structured_output")
    if not isinstance(structured, dict):
        raise RuntimeError(
            f"workflow step '{step.step_id}' response structured_output is required"
        )
    # A JSON-schema output is already shaped by the provider; only a pydantic
    # output model re-validates.
    if isinstance(step.output, dict):
        return dict(structured)
    return step.output.model_validate(structured).model_dump(mode="json")


def _agent_step_message(
    step: StepSpec,
    *,
    workflow_id: str,
    run_id: str,
    workflow_input: dict[str, Any],
    input_snapshot: dict[str, Any],
) -> str:
    """Envelope sent to an agent step.

    ``input`` is the coerced step input (``workflow_input`` merged with the prior
    step's output). Each run is a clean slate — there is no cross-run state. A
    ``skill_name`` adds the load-this-skill-first instructions.
    """
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "workflow_run_id": run_id,
        "step_id": step.step_id,
        "workflow_input": dict(workflow_input),
        "input": dict(input_snapshot),
    }
    skill_name = getattr(step, "skill_name", None)
    if skill_name:
        payload["skill_name"] = skill_name
        payload["workflow_task_instructions"] = [
            f'Your first action must be calling the Skill tool with arguments {{"name": "{skill_name}"}}.',
            "After the Skill tool returns, follow the loaded skill instructions.",
            "Execute only the step identified by step_id.",
        ]
    return json.dumps(payload, ensure_ascii=True)


class ForwardPipelineStrategy(WorkflowStrategy):
    """Default strategy: a linear, durable, observable forward pipeline."""

    async def run(self, ctx: WorkflowExecutionContext) -> dict[str, Any]:
        dbos_class, *_ = load_dbos_api()
        workflow: WorkflowSpec = ctx.workflow
        runner_id = ctx.runner_id
        run_id = ctx.run_id
        wf_id = workflow.workflow_id
        session_id = ctx.session_id or _run_session_id(run_id)

        await dbos_class.run_step_async(
            {"name": "run.start"},
            _mark_run_started,
            runner_id,
            run_id,
            wf_id,
            ctx.workflow_input,
            session_id,
        )

        prev_output: dict[str, Any] | None = None
        result: dict[str, Any] = {}
        for ordinal, step in enumerate(workflow.steps):
            merged = {**ctx.workflow_input, **(prev_output or {})}
            input_snapshot: dict[str, Any] = merged
            try:
                input_snapshot = _coerce_input(step, merged)
                agent_request_id: str | None = None
                if step.kind == "agent":
                    agent_request_id = await post_inline_agent_request(
                        runner_id,
                        agent_id=step.agent_id,  # type: ignore[attr-defined]
                        message=_agent_step_message(
                            step,
                            workflow_id=wf_id,
                            run_id=run_id,
                            workflow_input=ctx.workflow_input,
                            input_snapshot=input_snapshot,
                        ),
                        structured_output=serialize_structured_output(step.output),
                        workflow_id=wf_id,
                        workflow_run_id=run_id,
                        task_id=step.step_id,
                        session_id=session_id,
                    )

                await dbos_class.run_step_async(
                    {"name": f"{step.step_id}.record.start"},
                    _record_step,
                    runner_id, run_id, wf_id, step.step_id, ordinal, step.kind,
                    STEP_RUNNING, input_snapshot, None, None, agent_request_id,
                    STEP_EVENT_STARTED, None,
                )

                if step.kind == "code":
                    if getattr(step, "orchestration", False):
                        # Orchestration code must run in the workflow context so
                        # it can start durable child agent workflows. It is
                        # replayed after recovery and therefore owns a durable,
                        # idempotent work ledger rather than relying on DBOS to
                        # memoize the code body as one opaque step.
                        output_snapshot = await _invoke_code_step(
                            runner_id,
                            run_id,
                            wf_id,
                            ordinal,
                            ctx.workflow_input,
                            input_snapshot,
                        )
                    else:
                        output_snapshot = await dbos_class.run_step_async(
                            {"name": f"{step.step_id}.run"},
                            _run_code_step,
                            runner_id,
                            run_id,
                            wf_id,
                            ordinal,
                            ctx.workflow_input,
                            input_snapshot,
                        )
                else:
                    payload = await _with_timeout(
                        dbos_class.run_step_async(
                            {"name": f"{step.step_id}.await"},
                            collect_terminal_payload,
                            runner_id, step.agent_id, agent_request_id,  # type: ignore[attr-defined]
                        ),
                        step.timeout_s,
                        step.step_id,
                    )
                    output_snapshot = _extract_agent_output(step, payload)

                await dbos_class.run_step_async(
                    {"name": f"{step.step_id}.record.done"},
                    _record_step,
                    runner_id, run_id, wf_id, step.step_id, ordinal, step.kind,
                    STEP_COMPLETED, input_snapshot, output_snapshot, None,
                    agent_request_id, STEP_EVENT_COMPLETED, None,
                )
            except Exception as exc:
                error = str(exc)
                await dbos_class.run_step_async(
                    {"name": f"{step.step_id}.record.fail"},
                    _record_step,
                    runner_id, run_id, wf_id, step.step_id, ordinal, step.kind,
                    STEP_FAILED, input_snapshot, None, error, None,
                    STEP_EVENT_FAILED, {"error": error},
                )
                await dbos_class.run_step_async(
                    {"name": "run.fail"},
                    _finish_run,
                    runner_id, run_id, RUN_FAILED, None, error,
                )
                raise

            prev_output = output_snapshot
            result = output_snapshot

        await dbos_class.run_step_async(
            {"name": "run.done"},
            _finish_run,
            runner_id, run_id, RUN_COMPLETED, result, None,
        )
        return {
            "workflow_id": wf_id,
            "run_id": run_id,
            "result": result,
            "completed_at": time.time(),
        }


FORWARD_PIPELINE_STRATEGY = ForwardPipelineStrategy()


__all__ = ["ForwardPipelineStrategy", "FORWARD_PIPELINE_STRATEGY"]

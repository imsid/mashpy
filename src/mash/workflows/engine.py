"""Forward-pipeline workflow engine.

Runs a ``WorkflowSpec.steps`` pipeline: step *n*'s output threads into step
*n+1*'s input (merged over the immutable ``workflow_input``), coerced and
validated through pydantic models at each boundary. The final step's output is
the run result.

A run's durable state is its rows in the workflow store. Each step records its
input and output in ``workflow_steps``, so a run that stopped (a host crash, a
failed step that was resumed) continues from them: a ``completed`` step is
skipped and its stored output feeds the next step. A step that did not
complete runs again under a new attempt, unless it was running when the host
died, in which case it keeps its attempt and an agent step reattaches to the
request it started.

``WorkflowRunner`` runs each run as a task on the host, at most
``MAX_CONCURRENT_RUNS`` at a time, and continues the deployment's unfinished
runs when the host starts.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mash.runtime.structured_output import serialize_structured_output

from .agents import collect_terminal_payload, post_inline_agent_request
from .runs import run_session_id, runner_run_id_prefix
from .spec import CodeStep, StepContext, StepSpec, WorkflowSpec
from .store import (
    RUN_COMPLETED,
    RUN_FAILED,
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

if TYPE_CHECKING:
    from mash.runtime.host.host import Pool

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 8
# A run continued this many times after a crash without finishing fails.
MAX_RECOVERIES = 10


@dataclass(frozen=True)
class WorkflowExecutionContext:
    """Inputs the engine needs to execute one workflow run."""

    pool: "Pool"
    workflow: WorkflowSpec
    run_id: str
    workflow_input: dict[str, Any]
    session_id: str | None = None
    user_id: str | None = None


def _store(pool: "Pool") -> WorkflowStore:
    store = getattr(pool, "get_workflow_store", lambda: None)()
    if store is None:
        raise RuntimeError("workflow store is not available on this pool")
    return store


async def _with_timeout(awaitable: Any, timeout_s: float | None, step_id: str) -> Any:
    if timeout_s is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"workflow step '{step_id}' exceeded its {timeout_s}s timeout"
        ) from exc


async def _record_step(
    store: WorkflowStore,
    *,
    run_id: str,
    workflow_id: str,
    step: StepSpec,
    ordinal: int,
    attempt: int,
    status: str,
    input_snapshot: dict[str, Any] | None,
    output_snapshot: dict[str, Any] | None,
    error: str | None,
    agent_request_id: str | None,
    event_type: str,
    event_payload: dict[str, Any] | None,
) -> None:
    now = time.time()
    await store.upsert_step(
        WorkflowStepRecord(
            run_id=run_id,
            workflow_id=workflow_id,
            step_id=step.step_id,
            ordinal=ordinal,
            kind=step.kind,
            status=status,
            input_snapshot=input_snapshot,
            output_snapshot=output_snapshot,
            error=error,
            attempt=attempt,
            agent_request_id=agent_request_id,
            started_at=now if status == STEP_RUNNING else None,
            finished_at=now if status in (STEP_COMPLETED, STEP_FAILED) else None,
        )
    )
    await store.append_step_event(
        run_id=run_id,
        workflow_id=workflow_id,
        step_id=step.step_id,
        event_type=event_type,
        at=now,
        attempt=attempt,
        payload=event_payload,
    )


async def _run_code_step(
    ctx: WorkflowExecutionContext,
    step: CodeStep,
    input_snapshot: dict[str, Any],
    attempt: int,
) -> dict[str, Any]:
    inp = step.input.model_validate(input_snapshot)
    step_ctx = StepContext(
        run_id=ctx.run_id,
        step_id=step.step_id,
        workflow_input=dict(ctx.workflow_input),
        attempt=attempt,
        user_id=ctx.user_id,
        browsers=getattr(ctx.pool, "get_browsers", lambda: None)(),
    )

    async def _invoke() -> Any:
        result = step.run(inp, step_ctx)
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
    step's output). Each run is a clean slate; there is no cross-run state. A
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


def _step_attempt(existing: WorkflowStepRecord | None) -> int:
    """1 for a new step; the same attempt for one the host died in; else the next."""
    if existing is None:
        return 1
    if existing.status == STEP_RUNNING:
        return max(1, int(existing.attempt))
    return max(1, int(existing.attempt)) + 1


class ForwardPipelineStrategy:
    """The one shape every workflow runs: a linear forward pipeline."""

    async def run(self, ctx: WorkflowExecutionContext) -> dict[str, Any]:
        store = _store(ctx.pool)
        workflow = ctx.workflow
        run_id = ctx.run_id
        wf_id = workflow.workflow_id
        session_id = ctx.session_id or run_session_id(run_id)

        await store.mark_run_started(run_id, time.time())
        recorded = {step.step_id: step for step in await store.get_run_steps(run_id)}

        prev_output: dict[str, Any] | None = None
        result: dict[str, Any] = {}
        for ordinal, step in enumerate(workflow.steps):
            existing = recorded.get(step.step_id)
            if existing is not None and existing.status == STEP_COMPLETED:
                prev_output = dict(existing.output_snapshot or {})
                result = prev_output
                continue

            attempt = _step_attempt(existing)
            merged = {**ctx.workflow_input, **(prev_output or {})}
            input_snapshot: dict[str, Any] = merged
            # Reset per step and before the try: the failure path records this,
            # and a step that fails in _coerce_input must not carry the
            # previous iteration's request id into its own row.
            agent_request_id: str | None = None
            record = {
                "run_id": run_id,
                "workflow_id": wf_id,
                "step": step,
                "ordinal": ordinal,
                "attempt": attempt,
            }
            try:
                input_snapshot = _coerce_input(step, merged)
                if step.kind == "agent":
                    agent_request_id = await post_inline_agent_request(
                        ctx.pool,
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
                        user_id=ctx.user_id,  # type: ignore[arg-type]
                        attempt=attempt,
                    )

                await _record_step(
                    store,
                    **record,
                    status=STEP_RUNNING,
                    input_snapshot=input_snapshot,
                    output_snapshot=None,
                    error=None,
                    agent_request_id=agent_request_id,
                    event_type=STEP_EVENT_STARTED,
                    event_payload=None,
                )

                if isinstance(step, CodeStep):
                    output_snapshot = await _run_code_step(ctx, step, input_snapshot, attempt)
                else:
                    payload = await _with_timeout(
                        collect_terminal_payload(
                            ctx.pool, step.agent_id, agent_request_id  # type: ignore[attr-defined,arg-type]
                        ),
                        step.timeout_s,
                        step.step_id,
                    )
                    output_snapshot = _extract_agent_output(step, payload)

                await _record_step(
                    store,
                    **record,
                    status=STEP_COMPLETED,
                    input_snapshot=input_snapshot,
                    output_snapshot=output_snapshot,
                    error=None,
                    agent_request_id=agent_request_id,
                    event_type=STEP_EVENT_COMPLETED,
                    event_payload=None,
                )
            except Exception as exc:
                error = str(exc)
                await _record_step(
                    store,
                    **record,
                    status=STEP_FAILED,
                    input_snapshot=input_snapshot,
                    output_snapshot=None,
                    error=error,
                    agent_request_id=agent_request_id,
                    event_type=STEP_EVENT_FAILED,
                    event_payload={"error": error},
                )
                await store.finish_run(
                    run_id, status=RUN_FAILED, error=error, finished_at=time.time()
                )
                raise

            prev_output = output_snapshot
            result = output_snapshot

        await store.finish_run(
            run_id, status=RUN_COMPLETED, result=result, finished_at=time.time()
        )
        return {
            "workflow_id": wf_id,
            "run_id": run_id,
            "result": result,
            "completed_at": time.time(),
        }


FORWARD_PIPELINE_STRATEGY = ForwardPipelineStrategy()


class WorkflowRunner:
    """Runs a pool's workflow runs as tasks on the host."""

    def __init__(self, pool: "Pool", *, runner_id: str) -> None:
        self._pool = pool
        self._runner_id = runner_id
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def is_running(self, run_id: str) -> bool:
        return run_id in self._tasks

    def start(self, run: WorkflowRunRecord) -> None:
        """Run ``run``, whose row is ``queued`` or ``running``, unless it already runs."""
        if run.run_id in self._tasks:
            return
        workflow = self._pool.get_workflow_registry().get(run.workflow_id)
        ctx = WorkflowExecutionContext(
            pool=self._pool,
            workflow=workflow,
            run_id=run.run_id,
            workflow_input=dict(run.workflow_input or {}),
            session_id=run.session_id,
            user_id=run.user_id,
        )
        task = asyncio.create_task(self._run(ctx), name=f"mash-workflow-{run.run_id}")
        self._tasks[run.run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run.run_id, None))

    async def recover(self) -> None:
        """Continue this runner's queued and running runs."""
        store = _store(self._pool)
        registry = self._pool.get_workflow_registry()
        for run in await store.list_unfinished_runs(runner_run_id_prefix(self._runner_id)):
            if run.run_id in self._tasks:
                continue
            try:
                registry.get(run.workflow_id)
            except KeyError:
                await store.finish_run(
                    run.run_id,
                    status=RUN_FAILED,
                    error=f"workflow '{run.workflow_id}' is no longer registered",
                    finished_at=time.time(),
                )
                continue
            if await store.count_recovery(run.run_id) > MAX_RECOVERIES:
                await store.finish_run(
                    run.run_id,
                    status=RUN_FAILED,
                    error=(
                        f"the run did not finish after {MAX_RECOVERIES} recoveries; "
                        "start a new run"
                    ),
                    finished_at=time.time(),
                )
                continue
            self.start(run)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        # A cancelled run stays running in the store; the next start continues it.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, ctx: WorkflowExecutionContext) -> None:
        async with self._slots:
            try:
                await FORWARD_PIPELINE_STRATEGY.run(ctx)
            except Exception:
                # The pipeline recorded the failure on the run.
                logger.info("workflow run %s failed", ctx.run_id, exc_info=True)


__all__ = [
    "FORWARD_PIPELINE_STRATEGY",
    "ForwardPipelineStrategy",
    "MAX_CONCURRENT_RUNS",
    "WorkflowExecutionContext",
    "WorkflowRunner",
]

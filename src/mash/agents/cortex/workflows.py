"""The cortex-build workflow: build one user's Cortex from their turns.

```text
collect (code) -> curate (code, orchestrates cortex-agent) -> write (code)
```

``collect`` reads the user's Cortex and their new non-workflow, non-subagent
turns with the tools each turn ran. ``curate`` sends them to ``cortex-agent``,
or skips the model when there are no new turns. ``write`` applies the result
in one transaction, as a compare-and-set on the version ``collect`` read.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from mash.runtime.events import RuntimeEventType
from mash.runtime.requests import validate_user_id
from mash.runtime.structured_output import serialize_structured_output
from mash.workflows import CodeStep, StepContext, WorkflowSpec
from mash.workflows.dbos import collect_terminal_payload, post_inline_agent_request

from .context import CortexRuntimeContext
from .models import EntryChange
from .spec import CORTEX_AGENT_ID
from .tools import SEARCH_CORTEX_TOOL_NAME

CORTEX_BUILD_WORKFLOW_ID = "cortex-build"

_MESSAGE_MAX_CHARS = 4_000
_TOOL_ARGUMENTS_MAX_CHARS = 500
_TOOL_RESULT_MAX_CHARS = 1_000
_TOOL_CALLS_PER_TURN = 20
# The whole bundle sent to the curator. Turns past it wait for the next run.
_BUNDLE_MAX_CHARS = 60_000

_TOOL_RESULT_EVENTS = {
    RuntimeEventType.TOOL_CALL_COMPLETED.value,
    RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
}


class CortexBuildInput(BaseModel):
    user_id: str
    limit: int = Field(default=200, ge=1, le=1000)


class EntrySource(BaseModel):
    agent_id: str
    session_id: str
    trace_id: str


class BundleEntry(BaseModel):
    entry_id: str
    topic: str
    content: str
    origin: str


class BundleToolCall(BaseModel):
    tool_name: str
    arguments: str
    result: str


class BundleTurn(BaseModel):
    agent_id: str
    session_id: str
    trace_id: str
    created_at: float
    user_message: str
    agent_response: str
    tool_calls: list[BundleToolCall] = Field(default_factory=list)


class CortexBundle(BaseModel):
    user_id: str
    base_version: int
    # created_at of the last turn in the bundle: the watermark after this build
    through: float
    remaining: int
    profile: str
    entries: list[BundleEntry]
    turns: list[BundleTurn]


class CuratedEntry(BaseModel):
    entry_id: str | None
    topic: str
    content: str
    sources: list[EntrySource]


class CuratorOutput(BaseModel):
    profile: str
    upserts: list[CuratedEntry]
    deletes: list[str]


class CortexCuration(BaseModel):
    user_id: str
    base_version: int
    through: float
    remaining: int
    turns_covered: int
    curated: bool
    profile: str
    upserts: list[CuratedEntry]
    deletes: list[str]


class CortexBuildResult(BaseModel):
    user_id: str
    turns_covered: int
    entries_written: int
    entries_deleted: int
    remaining: int


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


async def _tool_calls(runtime_store: Any, turn: dict[str, Any]) -> list[BundleToolCall]:
    events = await runtime_store.list_events(turn["app_id"], trace_id=turn["trace_id"])
    arguments: dict[str, Any] = {}
    calls: list[BundleToolCall] = []
    for event in events:
        if event.workflow_id is not None:
            continue
        payload = event.payload or {}
        if event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
            for call in payload.get("tool_calls") or []:
                arguments[str(call.get("id") or "")] = call.get("arguments") or {}
        elif event.event_type in _TOOL_RESULT_EVENTS:
            name = str(payload.get("tool_name") or "")
            # Cortex's own reads would feed memory back into itself.
            if name == SEARCH_CORTEX_TOOL_NAME or len(calls) >= _TOOL_CALLS_PER_TURN:
                continue
            result = payload.get("result") or {}
            calls.append(
                BundleToolCall(
                    tool_name=name,
                    arguments=_clip(
                        arguments.get(str(payload.get("tool_call_id") or ""), {}),
                        _TOOL_ARGUMENTS_MAX_CHARS,
                    ),
                    result=_clip(result.get("content") or "", _TOOL_RESULT_MAX_CHARS),
                )
            )
    return calls


def _build_collect(context: CortexRuntimeContext):
    async def collect(inp: CortexBuildInput, _ctx: StepContext) -> CortexBundle:
        user_id = validate_user_id(inp.user_id)
        pool = context.require_pool()
        snapshot = await context.require_store().load(user_id)
        # cortex-agent keeps the default store, so this is the pool's shared
        # memory store, which holds every such agent's turns.
        memory_store = pool.get_memory_store(CORTEX_AGENT_ID)
        runtime_store = pool.get_runtime_store()
        if memory_store is None or runtime_store is None:
            raise RuntimeError("cortex-build requires the pool's shared stores")
        turns = await memory_store.list_user_turns(
            user_id, after=snapshot.built_through, limit=inp.limit
        )
        total = await memory_store.count_user_turns(user_id, after=snapshot.built_through)

        bundle_turns: list[BundleTurn] = []
        used = 0
        for turn in turns:
            bundle_turn = BundleTurn(
                agent_id=turn["app_id"],
                session_id=turn["session_id"],
                trace_id=turn["trace_id"],
                created_at=turn["created_at"],
                user_message=_clip(turn["user_message"], _MESSAGE_MAX_CHARS),
                agent_response=_clip(turn["agent_response"], _MESSAGE_MAX_CHARS),
                tool_calls=await _tool_calls(runtime_store, turn),
            )
            size = len(bundle_turn.model_dump_json())
            if bundle_turns and used + size > _BUNDLE_MAX_CHARS:
                break
            bundle_turns.append(bundle_turn)
            used += size

        return CortexBundle(
            user_id=user_id,
            base_version=snapshot.version,
            through=bundle_turns[-1].created_at if bundle_turns else snapshot.built_through,
            remaining=total - len(bundle_turns),
            profile=snapshot.profile,
            entries=[
                BundleEntry(
                    entry_id=entry.entry_id,
                    topic=entry.topic,
                    content=entry.content,
                    origin=entry.origin,
                )
                for entry in snapshot.entries
            ],
            turns=bundle_turns,
        )

    return collect


def _build_curate(context: CortexRuntimeContext):
    async def curate(inp: CortexBundle, ctx: StepContext) -> CortexCuration:
        passthrough = dict(
            user_id=inp.user_id,
            base_version=inp.base_version,
            through=inp.through,
            remaining=inp.remaining,
            turns_covered=len(inp.turns),
        )
        if not inp.turns:
            return CortexCuration(
                **passthrough, curated=False, profile=inp.profile, upserts=[], deletes=[]
            )
        pool = context.require_pool()
        message = json.dumps(
            {
                "profile_max_chars": context.settings.profile_max_chars,
                "profile": inp.profile,
                "entries": [entry.model_dump() for entry in inp.entries],
                "turns": [turn.model_dump() for turn in inp.turns],
            },
            ensure_ascii=True,
        )
        # The request id is fixed by run and task, so a replay of this
        # orchestration step reattaches to the same curator request.
        request_id = await post_inline_agent_request(
            pool.runner_id,
            agent_id=CORTEX_AGENT_ID,
            message=message,
            structured_output=serialize_structured_output(CuratorOutput),
            workflow_id=CORTEX_BUILD_WORKFLOW_ID,
            workflow_run_id=ctx.run_id,
            task_id="curate",
            session_id=f"cortex:{ctx.run_id}",
        )
        payload = await collect_terminal_payload(pool.runner_id, CORTEX_AGENT_ID, request_id)
        structured = (payload.get("response") or {}).get("structured_output")
        if not isinstance(structured, dict):
            raise RuntimeError("cortex-agent completed without structured output")
        output = CuratorOutput.model_validate(structured)
        return CortexCuration(
            **passthrough,
            curated=True,
            profile=output.profile,
            upserts=output.upserts,
            deletes=output.deletes,
        )

    return curate


def _build_write(context: CortexRuntimeContext):
    async def write(inp: CortexCuration, ctx: StepContext) -> CortexBuildResult:
        result = {
            "user_id": inp.user_id,
            "turns_covered": inp.turns_covered,
            "remaining": inp.remaining,
        }
        if not inp.curated:
            return CortexBuildResult(**result, entries_written=0, entries_deleted=0)
        max_chars = context.settings.profile_max_chars
        applied = await context.require_store().apply_build(
            user_id=inp.user_id,
            run_id=ctx.run_id,
            base_version=inp.base_version,
            built_through=inp.through,
            profile=inp.profile.strip()[:max_chars],
            upserts=[
                EntryChange(
                    entry_id=entry.entry_id,
                    topic=entry.topic,
                    content=entry.content,
                    sources=[source.model_dump() for source in entry.sources],
                )
                for entry in inp.upserts
            ],
            deletes=list(inp.deletes),
            result=result,
        )
        return CortexBuildResult(**applied)

    return write


def build_cortex_build_workflow(context: CortexRuntimeContext) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=CORTEX_BUILD_WORKFLOW_ID,
        input_model=CortexBuildInput,
        steps=[
            CodeStep(
                step_id="collect",
                run=_build_collect(context),
                input=CortexBuildInput,
                output=CortexBundle,
            ),
            CodeStep(
                step_id="curate",
                run=_build_curate(context),
                input=CortexBundle,
                output=CortexCuration,
                agent_ids=[CORTEX_AGENT_ID],
                orchestration=True,
            ),
            CodeStep(
                step_id="write",
                run=_build_write(context),
                input=CortexCuration,
                output=CortexBuildResult,
            ),
        ],
    )


__all__ = [
    "CORTEX_BUILD_WORKFLOW_ID",
    "CortexBuildInput",
    "CortexBuildResult",
    "CuratorOutput",
    "build_cortex_build_workflow",
]

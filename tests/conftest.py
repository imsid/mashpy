from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT / "src", _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from mash.logging import bound_host_id, bound_request_id
from mash.runtime import context as context_helpers
from mash.runtime.requests import host_id_from_request_metadata
from mash.runtime.engine.steps import (
    commit_request_step,
    complete_request,
    fail_request,
    finalize_structured_output,
    load_request_context,
    persist_completed_turn,
    plan_request_step,
    run_step_tool_call,
    start_request_trace,
)
from mash.runtime.events.types import FeedbackRecord, RuntimeEvent, RuntimeEventType


def _event_tokens(event: RuntimeEvent) -> int:
    """Input+output tokens on one event, mirroring the Postgres token SUM."""
    payload = event.payload or {}
    usage = payload.get("token_usage") or {}
    inp = usage.get("input") or usage.get("input_tokens") or payload.get("input_tokens") or 0
    out = usage.get("output") or usage.get("output_tokens") or payload.get("output_tokens") or 0
    try:
        return int(inp) + int(out)
    except (TypeError, ValueError):
        return 0


class _TestRuntimeStore:
    def __init__(self, _database_url: str) -> None:
        self._events: list[RuntimeEvent] = []
        self._events_by_request: dict[str, list[RuntimeEvent]] = {}
        self._lock = asyncio.Lock()
        self._request_waiters: dict[str, set[asyncio.Event]] = {}
        self._global_waiters: set[asyncio.Event] = set()
        self._feedback: list[FeedbackRecord] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self._events.clear()
        self._events_by_request.clear()

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        async with self._lock:
            request_events = (
                self._events_by_request.setdefault(event.request_id, [])
                if event.request_id
                else None
            )
            if event.request_id and event.dedupe_key and request_events is not None:
                for existing in request_events:
                    if existing.dedupe_key == event.dedupe_key:
                        return existing
            stored = RuntimeEvent(
                event_id=len(self._events) + 1,
                request_id=event.request_id,
                request_seq=(
                    len(request_events) + 1 if request_events is not None else None
                ),
                trace_id=event.trace_id,
                app_id=event.app_id,
                agent_id=event.agent_id,
                session_id=event.session_id,
                host_id=event.host_id,
                workflow_id=event.workflow_id,
                workflow_run_id=event.workflow_run_id,
                event_type=event.event_type,
                loop_index=event.loop_index,
                step_key=event.step_key,
                dedupe_key=event.dedupe_key,
                payload=dict(event.payload or {}),
                created_at=float(event.created_at),
            )
            self._events.append(stored)
            if request_events is not None:
                request_events.append(stored)
        self._wake_waiters(event.request_id)
        return stored

    async def list_request_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]:
        async with self._lock:
            events = list(self._events_by_request.get(request_id, ()))
        return [
            event
            for event in events
            if int(event.request_seq or 0) > int(after_seq)
        ]

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        host_id: str | None = None,
        workflow_run_id: str | None = None,
        event_type_prefix: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        async with self._lock:
            events = list(self._events)
        filtered = [
            event
            for event in events
            if event.app_id == app_id
            and event.event_id > int(after_event_id)
            and (session_id is None or event.session_id == session_id)
            and (trace_id is None or event.trace_id == trace_id)
            and (host_id is None or event.host_id == host_id)
            and (workflow_run_id is None or event.workflow_run_id == workflow_run_id)
            and (event_type_prefix is None or event.event_type.startswith(event_type_prefix))
        ]
        if limit is not None:
            return filtered[-max(1, int(limit)) :]
        return filtered

    async def has_request(self, request_id: str) -> bool:
        async with self._lock:
            return request_id in self._events_by_request

    async def is_request_terminal(self, request_id: str) -> bool:
        async with self._lock:
            events = self._events_by_request.get(request_id, ())
            if not events:
                return False
            event_type = events[-1].event_type
        return event_type in {
            RuntimeEventType.REQUEST_COMPLETED.value,
            RuntimeEventType.REQUEST_FAILED.value,
        }

    async def append_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        async with self._lock:
            stored = FeedbackRecord(
                feedback_id=len(self._feedback) + 1,
                feedback_type=feedback.feedback_type,
                message=feedback.message,
                app_id=feedback.app_id,
                host_id=feedback.host_id,
                session_id=feedback.session_id,
                request_id=feedback.request_id,
                trace_id=feedback.trace_id,
                context=dict(feedback.context or {}),
                created_at=float(feedback.created_at),
            )
            self._feedback.append(stored)
        return stored

    async def list_feedback(
        self,
        app_id: str,
        *,
        after: float,
        before: float | None = None,
        feedback_type: str | None = None,
        session_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackRecord]:
        async with self._lock:
            records = list(self._feedback)
        query_term = (q or "").strip().lower()
        filtered = [
            record
            for record in records
            if record.app_id == app_id
            and record.created_at > float(after)
            and (before is None or record.created_at < float(before))
            and (feedback_type is None or record.feedback_type == feedback_type)
            and (session_id is None or record.session_id == session_id)
            and (not query_term or query_term in record.message.lower())
        ]
        filtered.sort(key=lambda item: (item.created_at, item.feedback_id), reverse=True)
        if limit is not None:
            return filtered[: max(1, int(limit))]
        return filtered

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        traces = await self.list_recent_traces(app_id, session_id=session_id, limit=1)
        return traces[0] if traces else None

    async def list_recent_traces(
        self,
        app_id: str | None = None,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        del host_id
        async with self._lock:
            events = list(self._events)
        grouped: dict[tuple[str, str | None], list[RuntimeEvent]] = {}
        for event in events:
            if (app_id is not None and event.app_id != app_id) or event.trace_id is None:
                continue
            if session_id is not None and event.session_id != session_id:
                continue
            grouped.setdefault((event.trace_id, event.session_id), []).append(event)
        summaries: list[dict[str, Any]] = []
        for (trace_id_value, session_id_value), trace_events in grouped.items():
            trace_events.sort(key=lambda item: item.event_id)
            summaries.append(
                {
                    "trace_id": trace_id_value,
                    "session_id": session_id_value,
                    "host_id": next(
                        (e.host_id for e in trace_events if e.host_id), None
                    ),
                    "agent_id": next(
                        (e.agent_id for e in trace_events if e.agent_id), None
                    ),
                    "workflow_id": next(
                        (e.workflow_id for e in trace_events if e.workflow_id), None
                    ),
                    "workflow_run_id": next(
                        (e.workflow_run_id for e in trace_events if e.workflow_run_id),
                        None,
                    ),
                    "event_count": len(trace_events),
                    "total_tokens": sum(_event_tokens(e) for e in trace_events),
                    "started_at": float(trace_events[0].created_at),
                    "latest_event_at": float(trace_events[-1].created_at),
                    "latest_event_id": int(trace_events[-1].event_id),
                }
            )
        summaries.sort(
            key=lambda item: (item["latest_event_at"], item["latest_event_id"]),
            reverse=True,
        )
        return summaries[: max(1, int(limit))]

    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            events = list(self._events)
        by_session: dict[str, list[RuntimeEvent]] = {}
        for event in events:
            if event.session_id is None:
                continue
            by_session.setdefault(event.session_id, []).append(event)


        sessions: list[dict[str, Any]] = []
        for session_id, session_events in by_session.items():
            ordered = sorted(session_events, key=lambda e: (e.created_at, e.event_id))
            owner = next((e.agent_id for e in ordered if e.agent_id), None)
            agent_ids = sorted({e.agent_id for e in ordered if e.agent_id})
            workflow_ids = sorted({e.workflow_id for e in ordered if e.workflow_id})
            # Participant semantics: match any agent that logged in the session.
            if agent_id is not None and agent_id not in agent_ids:
                continue
            if workflow_id is not None and workflow_id not in workflow_ids:
                continue
            trace_ids = {e.trace_id for e in ordered if e.trace_id is not None}
            sessions.append(
                {
                    "session_id": session_id,
                    "owner_agent_id": owner,
                    "agent_ids": agent_ids,
                    "workflow_ids": workflow_ids,
                    "host_id": next((e.host_id for e in ordered if e.host_id), None),
                    "started_at": float(ordered[0].created_at),
                    "latest_event_at": float(ordered[-1].created_at),
                    "trace_count": len(trace_ids),
                    "total_tokens": sum(_event_tokens(e) for e in session_events),
                }
            )
        sessions.sort(key=lambda s: (s["latest_event_at"], s["session_id"]), reverse=True)
        total = len(sessions)
        if limit is not None:
            sessions = sessions[: max(1, int(limit))]
        return {"sessions": sessions, "total": total}

    async def aggregate_workflow_activity(self) -> list[dict[str, Any]]:
        async with self._lock:
            events = list(self._events)
        by_workflow: dict[str, list[RuntimeEvent]] = {}
        for event in events:
            if event.workflow_id is None:
                continue
            by_workflow.setdefault(event.workflow_id, []).append(event)
        activity = []
        for workflow_id, wf_events in by_workflow.items():
            activity.append(
                {
                    "workflow_id": workflow_id,
                    "run_count": len({e.workflow_run_id for e in wf_events if e.workflow_run_id}),
                    "session_count": len({e.session_id for e in wf_events if e.session_id}),
                    "last_run_at": max(float(e.created_at) for e in wf_events),
                    "total_tokens": sum(_event_tokens(e) for e in wf_events),
                }
            )
        activity.sort(key=lambda a: a["last_run_at"], reverse=True)
        return activity

    def register_request_waiter(self, request_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._request_waiters.setdefault(request_id, set()).add(event)
        return event

    def unregister_request_waiter(
        self, request_id: str, event: asyncio.Event
    ) -> None:
        waiters = self._request_waiters.get(request_id)
        if waiters:
            waiters.discard(event)
            if not waiters:
                del self._request_waiters[request_id]

    def register_global_waiter(self) -> asyncio.Event:
        event = asyncio.Event()
        self._global_waiters.add(event)
        return event

    def unregister_global_waiter(self, event: asyncio.Event) -> None:
        self._global_waiters.discard(event)

    def _wake_waiters(self, request_id: str | None) -> None:
        if request_id:
            for ev in self._request_waiters.get(request_id, ()):
                ev.set()
        for ev in self._global_waiters:
            ev.set()


async def _execute_request_inline(
    runtime: Any,
    *,
    request_id: str,
    message: str,
    session_id: str,
    request_metadata: dict[str, Any],
) -> None:
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    session_id = session_id.strip()
    if not session_id:
        raise ValueError("session_id is required")
    trace_id: str | None = None
    with bound_request_id(request_id), bound_host_id(
        host_id_from_request_metadata(request_metadata)
    ):
        try:
            trace_id = await start_request_trace(
                runtime.app_id,
                request_id,
                session_id=session_id,
                message=message,
            )
            workflow_state = await load_request_context(
                runtime.app_id,
                request_id,
                session_id,
                trace_id,
                message,
                request_metadata,
            )
            while True:
                loop_index = int(workflow_state.get("loop_index") or 0)
                workflow_state = await plan_request_step(
                    runtime.app_id,
                    request_id,
                    session_id,
                    trace_id,
                    workflow_state,
                )
                event_payload = dict(workflow_state.get("action") or {})
                tool_calls = context_helpers.tool_calls_from_action_payload(event_payload)

                for tool_call in tool_calls:
                    workflow_state = await run_step_tool_call(
                        runtime.app_id,
                        request_id,
                        session_id,
                        trace_id,
                        workflow_state,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": dict(tool_call.arguments or {}),
                        },
                    )
                workflow_state = await commit_request_step(
                    runtime.app_id,
                    request_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    workflow_state=workflow_state,
                )
                if not bool(workflow_state.get("done")):
                    continue

                structured_output_request = request_metadata.get(
                    "structured_output_request"
                )
                if isinstance(structured_output_request, dict):
                    workflow_state = await finalize_structured_output(
                        runtime.app_id,
                        request_id,
                        session_id,
                        trace_id,
                        workflow_state,
                        structured_output_request,
                    )
                turn_payload = await persist_completed_turn(
                    runtime.app_id,
                    request_id,
                    session_id,
                    trace_id,
                    message=message,
                    workflow_state=workflow_state,
                    request_metadata=request_metadata,
                )
                await complete_request(
                    runtime.app_id,
                    request_id,
                    session_id,
                    trace_id,
                    turn_payload,
                )
                return
        except Exception as exc:
            await fail_request(
                runtime.app_id,
                request_id,
                session_id,
                trace_id,
                {
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
            )


class _TestDBOSRequestEngine:
    def __init__(self, runtime: Any, *, database_url: str) -> None:
        self._runtime = runtime
        self._database_url = database_url
        self._tasks: set[asyncio.Task[None]] = set()

    async def open(self) -> None:
        from mash.runtime.engine.dbos import register_runtime

        register_runtime(self._runtime)
        return None

    async def close(self) -> None:
        from mash.runtime.engine.dbos import unregister_runtime

        if not self._tasks:
            unregister_runtime(self._runtime)
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        unregister_runtime(self._runtime)

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            _execute_request_inline(
                self._runtime,
                request_id=request_id,
                message=message,
                session_id=session_id,
                request_metadata=dict(request_metadata or {}),
            ),
            name=f"TestRequest-{self._runtime.app_id}-{request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class _TestMemoryStore:
    """Minimal in-memory MemoryStore for tests; accepts a database_url constructor."""

    def __init__(self, _database_url: str) -> None:
        self._turns: list[dict[str, Any]] = []
        self._logs: list[dict[str, Any]] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        self._turns.clear()
        self._logs.clear()

    async def save_logs(self, logs: list[dict[str, Any]]) -> None:
        self._logs.extend(logs)

    async def get_logs(
        self,
        app_id: str,
        session_id: str | None = None,
        trace_id: str | None = None,
        limit: int | None = None,
        after_log_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            r for r in self._logs
            if r.get("app_id") == app_id
            and (session_id is None or r.get("session_id") == session_id)
            and (trace_id is None or r.get("trace_id") == trace_id)
        ]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    async def get_latest_log_trace(
        self, app_id: str, session_id: str
    ) -> dict[str, Any] | None:
        return None

    async def list_recent_log_traces(
        self, app_id: str, session_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        return []

    async def save_turn(
        self,
        trace_id: str,
        session_id: str,
        app_id: str,
        user_message: str,
        agent_response: str,
        signals: dict[str, Any],
        session_total_tokens: int,
        metadata: dict[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        replayable: bool = True,
    ) -> str:
        self._turns.append(
            {
                "trace_id": trace_id,
                "session_id": session_id,
                "app_id": app_id,
                "user_message": user_message,
                "agent_response": agent_response,
                "signals": signals,
                "session_total_tokens": session_total_tokens,
                "metadata": metadata or {},
                "workflow_id": workflow_id,
                "workflow_run_id": workflow_run_id,
                "task_id": task_id,
                "replayable": replayable,
            }
        )
        return trace_id

    async def get_turns(
        self,
        session_id: str,
        app_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "trace_id": t["trace_id"],
                "user_message": t.get("user_message", ""),
                "agent_response": t.get("agent_response", ""),
                "session_total_tokens": t.get("session_total_tokens", 0),
                "replayable": t.get("replayable", True),
                "signals": t.get("signals") or {},
                "metadata": t.get("metadata") or {},
                "created_at": 0.0,
            }
            for t in self._turns
            if t["session_id"] == session_id and t["app_id"] == app_id
        ]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    async def list_workflow_turns(
        self,
        app_id: str,
        *,
        workflow_id: str,
        workflow_run_id: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> list[dict[str, Any]]:
        rows = [
            t for t in self._turns
            if t["app_id"] == app_id
            and t.get("workflow_id") == workflow_id
            and (workflow_run_id is None or t.get("workflow_run_id") == workflow_run_id)
        ]
        if sort_desc:
            rows = list(reversed(rows))
        rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def get_session_signals(
        self, session_id: str, app_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "trace_id": t["trace_id"],
                "created_at": 0.0,
                "signals": t.get("signals") or {},
            }
            for t in self._turns
            if t["session_id"] == session_id and t["app_id"] == app_id
        ]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    async def list_sessions(self, app_id: str) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for t in self._turns:
            if t["app_id"] != app_id:
                continue
            sid = t["session_id"]
            if sid not in seen:
                seen[sid] = {"session_id": sid, "app_id": app_id}
        return list(seen.values())

    async def get_latest_session(self, app_id: str) -> dict[str, Any] | None:
        return None

    async def get_latest_trace(
        self, app_id: str, session_id: str
    ) -> dict[str, Any] | None:
        traces = await self.list_recent_traces(app_id, session_id, limit=1)
        return traces[0] if traces else None

    async def list_recent_traces(
        self, app_id: str, session_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "trace_id": t["trace_id"],
                "session_id": t["session_id"],
                "user_message": t.get("user_message", ""),
                "agent_response": t.get("agent_response", ""),
                "metadata": t.get("metadata") or {},
                "created_at": 0.0,
            }
            for t in self._turns
            if t["app_id"] == app_id and t["session_id"] == session_id
        ]
        return list(reversed(rows))[:max(1, int(limit))]

    async def get_turn_by_ids(
        self, pairs: list[dict[str, str]], app_id: str
    ) -> list[dict[str, Any]] | None:
        return None

    async def keyword_search(
        self,
        column: Any,
        query_term: str,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        col = str(column)
        term = query_term.lower()
        results = []
        for t in self._turns:
            if session_id is not None and t["session_id"] != session_id:
                continue
            if app_id is not None and t["app_id"] != app_id:
                continue
            text = str(t.get(col, "") or "")
            if term in text.lower():
                results.append(
                    {
                        "trace_id": t["trace_id"],
                        "session_id": t["session_id"],
                        "preview": text,
                        "score": 1.0,
                        "created_at": 0.0,
                    }
                )
        return results[:max(0, int(limit))]

    async def semantic_search(
        self,
        column: Any,
        query_term: str,
        query_embedding: list[float] | None,
        limit: int,
        session_id: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return []


def build_test_stores(
    database_url: str = "postgresql://test/runtime",
) -> tuple[_TestRuntimeStore, _TestMemoryStore]:
    """Create test-appropriate runtime and memory stores."""
    return _TestRuntimeStore(database_url), _TestMemoryStore(database_url)


class _TestWorkflowStore:
    """Minimal in-memory WorkflowStore for tests against the fake database URL."""

    def __init__(self, _database_url: str) -> None:
        self._runs: dict[str, Any] = {}
        self._steps: dict[tuple[str, str], Any] = {}
        self._events: list[Any] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        self._runs.clear()
        self._steps.clear()
        self._events.clear()

    async def create_run(self, run: Any) -> None:
        self._runs.setdefault(run.run_id, run)

    async def mark_run_started(self, run_id: str, started_at: float) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            run.status = "running"
            run.started_at = run.started_at or started_at

    async def finish_run(self, run_id: str, *, status: str, result=None, error=None, finished_at: float) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            run.status = status
            run.result = result
            run.error = error
            run.finished_at = finished_at

    async def upsert_step(self, step: Any) -> None:
        self._steps[(step.run_id, step.step_id)] = step

    async def append_step_event(self, *, run_id, workflow_id, step_id, event_type, at, attempt=1, payload=None) -> int:
        seq = sum(1 for e in self._events if e.run_id == run_id and e.step_id == step_id) + 1
        from mash.workflows.store import WorkflowStepEventRecord

        self._events.append(
            WorkflowStepEventRecord(
                run_id=run_id, workflow_id=workflow_id, step_id=step_id,
                attempt=attempt, event_type=event_type, seq=seq, at=at, payload=payload or {},
            )
        )
        return seq

    async def get_run(self, run_id: str):
        return self._runs.get(run_id)

    async def list_runs(self, workflow_id: str, *, status=None, start_time=None, end_time=None, limit=50, offset=0, sort_desc=True):
        runs = [r for r in self._runs.values() if r.workflow_id == workflow_id]
        if status is not None:
            runs = [r for r in runs if r.status == status]
        runs.sort(key=lambda r: r.created_at, reverse=sort_desc)
        return runs[offset : offset + limit]

    async def get_run_steps(self, run_id: str):
        steps = [s for (rid, _), s in self._steps.items() if rid == run_id]
        steps.sort(key=lambda s: s.ordinal)
        return steps

    async def list_step_events(self, run_id: str, *, step_id=None, after_seq=0):
        events = [e for e in self._events if e.run_id == run_id]
        if step_id is not None:
            events = [e for e in events if e.step_id == step_id and e.seq > after_seq]
        events.sort(key=lambda e: (e.at, e.step_id, e.seq))
        return events


@pytest.fixture(autouse=True, scope="session")
def _patch_hosted_runtime_for_tests():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    patcher.setenv("MASH_DATABASE_URL", "postgresql://test/runtime")
    patcher.setattr("mash.runtime.host.host.PostgresRuntimeStore", _TestRuntimeStore)
    patcher.setattr("mash.runtime.host.host.PostgresStore", _TestMemoryStore)
    patcher.setattr("mash.runtime.host.host.WorkflowStore", _TestWorkflowStore)
    patcher.setattr("mash.runtime.server.PostgresRuntimeStore", _TestRuntimeStore)
    patcher.setattr("mash.runtime.service.DBOSRequestEngine", _TestDBOSRequestEngine)
    yield
    patcher.undo()

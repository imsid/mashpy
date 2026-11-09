"""Telemetry and observability API routes."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from mash.otel.loading import load_request_tree
from mash.otel.projection import project_request, serialize_projection
from mash.runtime.events import analyze_projection, serialize_runtime_event
from mash.runtime.events.types import RuntimeEvent

from .common import (
    APIError,
    CommandEventIngest,
    build_memory_search_service,
    get_runtime_store,
    normalize_optional_text,
    parse_limit,
    state_from_request,
    success,
    telemetry_event_source,
)


def build_telemetry_router() -> APIRouter:
    router = APIRouter()

    @router.get("/telemetry/events")
    async def get_observability_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        trace_id: Optional[str] = Query(default=None),
        host_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        store = get_runtime_store(request, agent_id)

        resolved_limit = parse_limit(limit, default=state.default_events_limit, max_value=20000)
        events = [
            serialize_runtime_event(item)
            for item in await store.list_events(
                app_id=agent_id,
                session_id=normalize_optional_text(session_id),
                trace_id=normalize_optional_text(trace_id),
                host_id=normalize_optional_text(host_id),
                limit=resolved_limit,
            )
        ]
        return success(
            {
                "events": events,
                "source": telemetry_event_source(),
                "agent_id": agent_id,
                "session_id": normalize_optional_text(session_id),
                "trace_id": normalize_optional_text(trace_id),
                "host_id": normalize_optional_text(host_id),
                "limit": resolved_limit,
            }
        )

    @router.get("/telemetry/memory/search")
    async def search_memory(
        request: Request,
        q: str,
        app_id: str,
        session_id: Optional[str] = None,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        query_text = q.strip()
        if not query_text:
            raise APIError(code="MISSING_QUERY", message="q is required", status_code=400, details={"param": "q"})

        app_id_value = app_id.strip()
        if not app_id_value:
            raise APIError(
                code="MISSING_APP_ID",
                message="app_id is required",
                status_code=400,
                details={"param": "app_id"},
            )

        memory_store = state.pool.get_memory_store(app_id_value)
        if memory_store is None:
            raise APIError(
                code="MEMORY_SEARCH_UNAVAILABLE",
                message="memory search unavailable for this agent",
                status_code=503,
            )

        search_service = build_memory_search_service(memory_store)
        resolved_limit = parse_limit(limit, default=state.default_search_limit, max_value=50)
        normalized_session_id = normalize_optional_text(session_id)
        try:
            results = await search_service.search(
                query_text,
                limit=resolved_limit,
                session_id=normalized_session_id,
                app_id=app_id_value,
            )
        except ValueError as exc:
            raise APIError(code="SEARCH_VALIDATION_ERROR", message=str(exc), status_code=400) from exc
        except (NotImplementedError, RuntimeError) as exc:
            raise APIError(code="SEARCH_UNAVAILABLE", message=str(exc), status_code=503) from exc
        except Exception as exc:  # pragma: no cover
            raise APIError(code="SEARCH_FAILED", message=f"search failed: {exc}", status_code=500) from exc

        return success(
            {
                "results": [asdict(result) for result in results],
                "app_id": app_id_value,
                "session_id": normalized_session_id,
                "query": query_text,
                "limit": resolved_limit,
            }
        )

    @router.get("/telemetry/traces")
    async def list_traces(
        request: Request,
        agent_id: Optional[str] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        host_id: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=5),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        resolved_agent_id = normalize_optional_text(agent_id)
        resolved_session_id = normalize_optional_text(session_id)
        if resolved_agent_id is None and resolved_session_id is None:
            raise APIError(
                code="INVALID_REQUEST",
                message="agent_id or session_id is required",
                status_code=400,
            )

        resolved_status = normalize_optional_text(status)
        if resolved_status is not None and resolved_status not in {
            "completed",
            "error",
            "in_progress",
        }:
            raise APIError(
                code="INVALID_STATUS",
                message="status must be 'completed', 'error', or 'in_progress'",
                status_code=400,
                details={"param": "status"},
            )

        # With a session but no agent, list a session's traces across the pool
        # (RunSubAgents children / cross-agent workflow tasks). Use any agent's shared store.
        if resolved_agent_id is not None:
            store = get_runtime_store(request, resolved_agent_id)
        else:
            store = state.pool.get_runtime_store()
            if store is None:
                raise APIError(code="RUNTIME_UNAVAILABLE", message="pool is not started", status_code=503)

        resolved_host_id = normalize_optional_text(host_id)
        traces = await store.list_recent_traces(
            resolved_agent_id,
            session_id=resolved_session_id,
            host_id=resolved_host_id,
            status=resolved_status,
            limit=max(1, min(limit, 100)),
        )
        return success(
            {
                "traces": traces,
                "agent_id": resolved_agent_id,
                "session_id": resolved_session_id,
                "host_id": resolved_host_id,
                "status": resolved_status,
            }
        )

    @router.get("/telemetry/usage")
    async def get_usage(
        request: Request,
        agent_id: str,
        host_id: Optional[str] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        bucket: str = Query(default="day"),
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
    ) -> dict[str, Any]:
        normalized_bucket = str(bucket or "day").strip().lower()
        if normalized_bucket not in {"hour", "day"}:
            raise APIError(
                code="INVALID_BUCKET",
                message="bucket must be 'hour' or 'day'",
                status_code=400,
                details={"param": "bucket"},
            )

        store = get_runtime_store(request, agent_id)

        resolved_host_id = normalize_optional_text(host_id)
        resolved_session_id = normalize_optional_text(session_id)
        buckets = await store.aggregate_usage(
            agent_id,
            host_id=resolved_host_id,
            session_id=resolved_session_id,
            bucket=normalized_bucket,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        return success(
            {
                "buckets": buckets,
                "agent_id": agent_id,
                "host_id": resolved_host_id,
                "session_id": resolved_session_id,
                "bucket": normalized_bucket,
                "from_ts": from_ts,
                "to_ts": to_ts,
            }
        )

    @router.get("/telemetry/tool-invocations")
    async def get_tool_invocations(
        request: Request,
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        invocations = await state.pool.aggregate_tool_invocations(from_ts=from_ts, to_ts=to_ts)
        return success({"invocations": invocations, "from_ts": from_ts, "to_ts": to_ts})

    @router.get("/telemetry/skill-invocations")
    async def get_skill_invocations(
        request: Request,
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        invocations = await state.pool.aggregate_skill_invocations(from_ts=from_ts, to_ts=to_ts)
        return success({"invocations": invocations, "from_ts": from_ts, "to_ts": to_ts})

    @router.get("/telemetry/sessions")
    async def list_sessions(
        request: Request,
        agent_id: Optional[str] = Query(default=None),
        workflow_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        participant_agent_id = normalize_optional_text(agent_id)
        resolved_workflow_id = normalize_optional_text(workflow_id)
        # The runtime event log is shared across the pool, so any agent's store
        # rolls up every session. agent_id matches sessions the agent
        # participated in (as the companion or a host agent); workflow_id matches
        # sessions where that workflow ran.
        store = state.pool.get_runtime_store()
        if store is None or not state.pool.list_agents():
            return success({"sessions": [], "total": 0, "agent_id": participant_agent_id})
        resolved_limit = (
            parse_limit(limit, default=state.default_events_limit, max_value=2000)
            if limit is not None
            else None
        )
        result = await store.list_sessions(
            agent_id=participant_agent_id,
            workflow_id=resolved_workflow_id,
            limit=resolved_limit,
        )
        return success(
            {
                "sessions": result["sessions"],
                "total": result["total"],
                "source": telemetry_event_source(),
                "agent_id": participant_agent_id,
                "workflow_id": resolved_workflow_id,
                "limit": resolved_limit,
            }
        )

    @router.post("/telemetry/command-events")
    async def ingest_command_event(
        request: Request,
        body: CommandEventIngest,
    ) -> dict[str, Any]:
        event_type = str(body.event_type or "").strip()
        if not event_type.startswith("command."):
            raise APIError(
                code="INVALID_EVENT_TYPE",
                message="event_type must start with 'command.'",
                status_code=400,
                details={"param": "event_type"},
            )

        agent_id = str(body.agent_id or "").strip()
        store = get_runtime_store(request, agent_id)

        event = RuntimeEvent(
            app_id=agent_id,
            agent_id=agent_id,
            event_type=event_type,
            session_id=normalize_optional_text(body.session_id),
            host_id=normalize_optional_text(body.host_id),
            trace_id=normalize_optional_text(body.trace_id),
            created_at=float(body.ts) if body.ts else time.time(),
            payload={
                "command_name": body.command_name,
                "args": body.args,
                "duration_ms": body.duration_ms,
                "error": body.error,
            },
        )
        stored = await store.append_event(event)
        return success({"event": serialize_runtime_event(stored)})

    @router.get("/telemetry/command-events")
    async def list_command_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        store = get_runtime_store(request, agent_id)

        resolved_limit = parse_limit(limit, default=state.default_events_limit, max_value=2000)
        events = [
            serialize_runtime_event(item)
            for item in await store.list_events(
                app_id=agent_id,
                session_id=normalize_optional_text(session_id),
                event_type_prefix="command.",
                limit=resolved_limit,
            )
        ]
        return success(
            {
                "events": events,
                "source": telemetry_event_source(),
                "agent_id": agent_id,
                "session_id": normalize_optional_text(session_id),
                "limit": resolved_limit,
            }
        )

    @router.get("/telemetry/trace/analysis")
    async def get_trace_analysis(
        request: Request,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """A trace's OpenTelemetry projection and the analysis built on it.

        The projection is the same spans and log records the exporter sends;
        RunSubAgents children are nested under the call that started them.
        """
        store = get_runtime_store(request, agent_id)
        request_id = await store.get_request_id_for_trace(trace_id, app_id=agent_id)
        if request_id is None:
            raise APIError(code="TRACE_NOT_FOUND", message=f"no events for trace {trace_id}", status_code=404)
        events, children = await load_request_tree(store, request_id)
        if not events:
            raise APIError(code="TRACE_NOT_FOUND", message=f"no events for trace {trace_id}", status_code=404)

        projection = project_request(events, children=children)
        analysis = analyze_projection(projection)

        return success(
            {
                "analysis": analysis.to_digest_dict(),
                "projection": serialize_projection(projection),
                "trace_id": trace_id,
                "request_id": request_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "status": analysis.status,
                "total_duration_ms": round(analysis.total_duration_ms, 3),
                "tokens": {
                    "input_tokens": analysis.input_tokens,
                    "output_tokens": analysis.output_tokens,
                    "cache_read_tokens": analysis.cache_read_tokens,
                    "cache_write_tokens": analysis.cache_write_tokens,
                },
                "counts": {
                    "step_count": analysis.step_count,
                    "tool_call_count": analysis.tool_call_count,
                    "tool_error_count": analysis.tool_error_count,
                    "event_count": len(events),
                },
            }
        )

    return router

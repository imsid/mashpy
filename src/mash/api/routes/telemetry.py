"""Telemetry and observability API routes."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from mash.api.logging import serialize_api_event
from mash.runtime.events import (
    analyze_trace,
    build_span_tree,
    serialize_runtime_event,
    serialize_span,
)
from mash.runtime.events.types import RuntimeEvent

from .common import (
    APIError,
    APIEventSearchRequest,
    CommandEventIngest,
    api_event_source,
    build_api_filters,
    build_memory_search_service,
    build_observability_sse_payload,
    memory_search_available,
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
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        resolved_limit = parse_limit(limit, default=state.default_events_limit, max_value=20000)
        events = [
            serialize_runtime_event(item)
            for item in await agent.runtime_store.list_events(
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

    @router.get("/telemetry/events/stream")
    async def stream_observability_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        trace_id: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        async def _generate() -> AsyncIterator[str]:
            resolved_session_id = normalize_optional_text(session_id)
            resolved_trace_id = normalize_optional_text(trace_id)
            latest = await agent.runtime_store.list_events(
                app_id=agent_id,
                session_id=resolved_session_id,
                trace_id=resolved_trace_id,
                limit=1,
            )
            last_seen = 0
            if latest:
                try:
                    last_seen = int(latest[-1].event_id or 0)
                except (TypeError, ValueError):
                    last_seen = 0

            while True:
                if await request.is_disconnected():
                    break
                events = await agent.runtime_store.list_events(
                    app_id=agent_id,
                    session_id=resolved_session_id,
                    trace_id=resolved_trace_id,
                    after_event_id=last_seen,
                )
                if not events:
                    yield ": keep-alive\n\n"
                    waiter = agent.runtime_store.register_global_waiter()
                    try:
                        try:
                            await asyncio.wait_for(waiter.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                    finally:
                        agent.runtime_store.unregister_global_waiter(waiter)
                    continue
                for event in events:
                    try:
                        last_seen = max(last_seen, int(event.event_id or 0))
                    except (TypeError, ValueError):
                        pass
                    yield build_observability_sse_payload(
                        json.dumps(serialize_runtime_event(event), ensure_ascii=True)
                    )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.get("/telemetry/api/events")
    async def get_api_events(
        request: Request,
        method: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        status_code: Optional[int] = Query(default=None),
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        after_event_id: int = Query(default=0),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)
        filters = build_api_filters(
            method=method,
            path=path,
            status_code=status_code,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit or state.default_events_limit,
            after_event_id=after_event_id,
        )
        events = [serialize_api_event(item) for item in await state.api_event_store.list_events(filters)]
        return success(
            {
                "events": events,
                "source": api_event_source(),
                "limit": filters.limit,
            }
        )

    @router.post("/telemetry/api/events/search")
    async def search_api_events(request: Request, body: APIEventSearchRequest) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)
        filters = build_api_filters(
            method=body.method,
            path=body.path,
            path_prefix=body.path_prefix,
            status_code=body.status_code,
            status_code_min=body.status_code_min,
            status_code_max=body.status_code_max,
            from_ts=body.from_ts,
            to_ts=body.to_ts,
            limit=body.limit or state.default_events_limit,
            after_event_id=body.after_event_id,
        )
        events = [serialize_api_event(item) for item in await state.api_event_store.list_events(filters)]
        return success(
            {
                "events": events,
                "source": api_event_source(),
                "limit": filters.limit,
            }
        )

    @router.get("/telemetry/api/events/stream")
    async def stream_api_events(
        request: Request,
        method: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        status_code: Optional[int] = Query(default=None),
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
    ) -> StreamingResponse:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        async def _generate() -> AsyncIterator[str]:
            latest_filters = build_api_filters(
                method=method,
                path=path,
                status_code=status_code,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=1,
            )
            latest = await state.api_event_store.list_events(latest_filters)
            last_seen = int(latest[-1].api_event_id) if latest else 0
            while True:
                if await request.is_disconnected():
                    break
                filters = build_api_filters(
                    method=method,
                    path=path,
                    status_code=status_code,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    after_event_id=last_seen,
                    limit=state.default_events_limit,
                )
                events = await state.api_event_store.list_events(filters)
                if not events:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.25)
                    continue
                for event in events:
                    last_seen = max(last_seen, int(event.api_event_id))
                    yield build_observability_sse_payload(
                        json.dumps(serialize_api_event(event), ensure_ascii=True)
                    )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
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
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

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

        try:
            agent = state.pool.get_agent(app_id_value)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        if not memory_search_available(agent):
            raise APIError(
                code="MEMORY_SEARCH_UNAVAILABLE",
                message="memory search unavailable for this agent",
                status_code=503,
            )

        search_service = build_memory_search_service(agent)
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
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        host_id: Optional[str] = Query(default=None),
        limit: int = Query(default=5),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        resolved_host_id = normalize_optional_text(host_id)
        traces = await agent.runtime_store.list_recent_traces(
            agent_id,
            session_id=normalize_optional_text(session_id),
            host_id=resolved_host_id,
            limit=max(1, min(limit, 100)),
        )
        return success(
            {"traces": traces, "agent_id": agent_id, "host_id": resolved_host_id}
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
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        normalized_bucket = str(bucket or "day").strip().lower()
        if normalized_bucket not in {"hour", "day"}:
            raise APIError(
                code="INVALID_BUCKET",
                message="bucket must be 'hour' or 'day'",
                status_code=400,
                details={"param": "bucket"},
            )

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        resolved_host_id = normalize_optional_text(host_id)
        resolved_session_id = normalize_optional_text(session_id)
        buckets = await agent.runtime_store.aggregate_usage(
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

    @router.post("/telemetry/command-events")
    async def ingest_command_event(
        request: Request,
        body: CommandEventIngest,
    ) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        event_type = str(body.event_type or "").strip()
        if not event_type.startswith("command."):
            raise APIError(
                code="INVALID_EVENT_TYPE",
                message="event_type must start with 'command.'",
                status_code=400,
                details={"param": "event_type"},
            )

        agent_id = str(body.agent_id or "").strip()
        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

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
        stored = await agent.runtime_store.append_event(event)
        return success({"event": serialize_runtime_event(stored)})

    @router.get("/telemetry/command-events")
    async def list_command_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        resolved_limit = parse_limit(limit, default=state.default_events_limit, max_value=2000)
        events = [
            serialize_runtime_event(item)
            for item in await agent.runtime_store.list_events(
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
        stitch: bool = False,
    ) -> dict[str, Any]:
        state = state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        events = await agent.runtime_store.list_events(
            app_id=agent_id,
            session_id=session_id,
            trace_id=trace_id,
        )
        if not events:
            raise APIError(code="TRACE_NOT_FOUND", message=f"no events for trace {trace_id}", status_code=404)

        tree = build_span_tree(events)
        analysis = analyze_trace(tree)

        if stitch and analysis.subagent_details:
            from mash.agents.masher.tool import _stitch_subagent_traces

            analysis = await _stitch_subagent_traces(agent.runtime_store, analysis)

        return success(
            {
                "analysis": analysis.to_digest_dict(),
                "span_tree": serialize_span(tree.root),
                "trace_id": trace_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "status": analysis.status,
                "total_duration_ms": round(analysis.total_duration_ms, 3),
                "tokens": {
                    "input_tokens": analysis.input_tokens,
                    "output_tokens": analysis.output_tokens,
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

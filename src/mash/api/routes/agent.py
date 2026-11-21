"""Agent and session API routes."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from mash.runtime import AgentClientError
from mash.runtime.client import (
    AgentUnavailableError,
)
from mash.runtime.host.host import AgentInUseError
from mash.runtime.host.processes import AgentStartError
from mash.runtime.events import build_reasoning_trace
from mash.memory.signals import build_default_signal_collector
from mash.runtime import sessions

from .common import (
    APIError,
    CompactSessionRequest,
    build_runtime_event_sse_payload,
    get_client,
    parse_limit,
    require_agent_id,
    require_session_id,
    require_trace_id,
    state_from_request,
    success,
    telemetry_event_source,
)
from .common import (
    get_runtime_store,
    normalize_optional_text,
)


logger = logging.getLogger(__name__)

_REQUEST_ID_KEYS = ("request_id", "workflow_id")
# GET /agent/{id}/sessions returns the most recently active sessions first.
SESSIONS_DEFAULT_LIMIT = 50
SESSIONS_MAX_LIMIT = 500


def _without_request_ids(payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
    """Re-key a request-scoped response onto the trace that addressed it.

    Request and workflow ids are runtime bookkeeping; a caller working in
    traces should not have to hold them to act on one.
    """
    return {
        "trace_id": trace_id,
        **{k: v for k, v in payload.items() if k not in _REQUEST_ID_KEYS},
    }


def build_agent_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success(
            {
                "status": "ok",
                "service": "mash-api",
                "api_version": "v1",
                "deployment": {
                    "agents": state.pool.describe_agents(),
                    "hosts": state.pool.describe_hosts(),
                },
            }
        )

    @router.get("/agent")
    async def list_agents(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success(
            {
                "agents": state.pool.describe_agents(),
                "hosts": state.pool.describe_hosts(),
            }
        )

    def _described_agent(request: Request, agent_id: str) -> dict[str, Any]:
        state = state_from_request(request)
        try:
            state.pool.require_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc
        described = {item["agent_id"]: item for item in state.pool.describe_agents()}
        # Workflow-only agents are not listed; describe them by id and status.
        return described.get(
            agent_id,
            {
                "agent_id": agent_id,
                "enabled": state.pool.is_agent_enabled(agent_id),
                "status": state.pool.agent_status(agent_id),
            },
        )

    @router.post("/agent/{agent_id}/enable")
    async def enable_agent(request: Request, agent_id: str) -> dict[str, Any]:
        state = state_from_request(request)
        _described_agent(request, agent_id)
        try:
            await state.pool.enable_agent(agent_id)
        except AgentStartError as exc:
            raise APIError(
                code="AGENT_UNAVAILABLE", message=str(exc), status_code=503
            ) from exc
        return success({"agent": _described_agent(request, agent_id)})

    @router.post("/agent/{agent_id}/disable")
    async def disable_agent(request: Request, agent_id: str) -> dict[str, Any]:
        state = state_from_request(request)
        _described_agent(request, agent_id)
        try:
            await state.pool.disable_agent(agent_id)
        except AgentInUseError as exc:
            raise APIError(
                code="AGENT_IN_USE",
                message=str(exc),
                status_code=409,
                details={"references": exc.references},
            ) from exc
        return success({"agent": _described_agent(request, agent_id)})

    @router.get("/agent/{agent_id}")
    async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
        return success({"agent": _described_agent(request, agent_id)})

    @router.get("/agent/{agent_id}/request/{request_id}/events")
    async def stream_request_events(
        request: Request,
        agent_id: str,
        request_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        """Stream the request's events as SSE, each with its seq as ``id:``.

        ``after`` is the seq of the last event the reader has: the stream
        starts at the next one, so a reader that lost its connection continues
        where it stopped. Without it the stream starts at the first event.
        """
        client = get_client(request, agent_id)
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="request_id is required",
                status_code=400,
            )

        async def _generate() -> AsyncIterator[str]:
            try:
                async for event in client.stream_response(
                    normalized_request_id, after=after
                ):
                    event_name = str(event.get("event") or "message")
                    payload = event.get("data")
                    yield build_runtime_event_sse_payload(
                        event_name, payload, seq=event.get("seq")
                    )
                    if event_name in {
                        "request.completed",
                        "request.error",
                        "request.cancelled",
                    }:
                        break
            except AgentClientError as exc:
                yield build_runtime_event_sse_payload(
                    "request.error",
                    {
                        "request_id": normalized_request_id,
                        "status": "error",
                        "error": str(exc),
                    },
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.get("/agent/{agent_id}/request/{request_id}/status")
    async def get_request_status(
        request: Request, agent_id: str, request_id: str
    ) -> dict[str, Any]:
        client = get_client(request, agent_id)
        normalized = request_id.strip()
        if not normalized:
            raise APIError(
                code="INVALID_REQUEST",
                message="request_id is required",
                status_code=400,
            )
        try:
            status = await client.get_request_status(normalized)
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(status)

    def _session_store(request: Request, agent_id: str):
        """The shared memory store holding ``agent_id``'s sessions."""
        state = state_from_request(request)
        try:
            resolved_agent_id = state.pool.require_agent(agent_id)
        except ValueError as exc:
            raise APIError(
                code="AGENT_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        store = state.pool.get_memory_store(resolved_agent_id)
        if store is None:
            raise APIError(
                code="SESSIONS_UNAVAILABLE",
                message=(
                    f"agent '{resolved_agent_id}' does not use the pool's shared "
                    "memory store"
                ),
                status_code=503,
            )
        return resolved_agent_id, store

    @router.get("/agent/{agent_id}/sessions")
    async def list_sessions(
        request: Request,
        agent_id: str,
        limit: Optional[int] = Query(default=None),
        app_id: Optional[str] = Query(default=None),
    ) -> dict[str, Any]:
        resolved_agent_id, store = _session_store(request, agent_id)
        resolved_limit = parse_limit(
            limit, default=SESSIONS_DEFAULT_LIMIT, max_value=SESSIONS_MAX_LIMIT
        )
        return success(
            {
                "sessions": await sessions.list_sessions(
                    store,
                    agent_id=resolved_agent_id,
                    limit=resolved_limit,
                    app_id=normalize_optional_text(app_id),
                ),
                "limit": resolved_limit,
            }
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}")
    async def get_runtime_session(
        request: Request, agent_id: str, session_id: str
    ) -> dict[str, Any]:
        resolved_agent_id, store = _session_store(request, agent_id)
        resolved_session_id = require_session_id(session_id)
        state = state_from_request(request)
        settings = state.pool.get_agent_model_settings(resolved_agent_id)
        return success(
            await sessions.session_info(
                store,
                state.pool.get_runtime_store(),
                agent_id=resolved_agent_id,
                session_id=resolved_session_id,
                model=settings["model"],
                max_steps=settings["max_steps"],
            )
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}/requests")
    async def list_session_requests(
        request: Request,
        agent_id: str,
        session_id: str,
        status: Optional[
            Literal["running", "completed", "failed", "cancelled"]
        ] = Query(default=None),
        started_by: Optional[str] = Query(default=None),
    ) -> dict[str, Any]:
        """The session's requests, newest first, from every agent in it."""
        store = get_runtime_store(request, require_agent_id(agent_id))
        records = await store.list_session_requests(
            require_session_id(session_id),
            status=status,
            started_by=started_by,
        )
        return success(
            {
                "requests": [
                    {
                        key: record.get(key)
                        for key in (
                            "request_id",
                            "agent_id",
                            "app_id",
                            "policy_id",
                            "message",
                            "status",
                            "started_by",
                            "parent_request_id",
                            "started_at",
                        )
                    }
                    for record in records
                ]
            }
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}/history")
    async def get_history(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        resolved_agent_id, store = _session_store(request, agent_id)
        if not session_id.strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        return success(
            {
                "turns": await sessions.history_turns(
                    store, agent_id=resolved_agent_id, session_id=session_id, limit=limit
                )
            }
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}/signals")
    async def get_session_signals(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        resolved_agent_id, store = _session_store(request, agent_id)
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        return success(
            {
                "agent_id": resolved_agent_id,
                "session_id": normalized_session_id,
                "definitions": build_default_signal_collector().get_signal_definitions(),
                "turns": await sessions.session_signals(
                    store,
                    agent_id=resolved_agent_id,
                    session_id=normalized_session_id,
                    limit=limit,
                ),
            }
        )

    @router.post("/agent/{agent_id}/sessions/{session_id}/compact")
    async def compact_session(
        request: Request,
        agent_id: str,
        session_id: str,
        body: CompactSessionRequest,
    ) -> dict[str, Any]:
        client = get_client(request, agent_id)
        if not session_id.strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        result = await client.compact_session(
            session_id.strip(),
            reason=body.reason,
            session_total_tokens_reset=body.session_total_tokens_reset,
        )
        return success(
            {"summary_text": result.get("summary_text"), "trace_id": result.get("trace_id")}
        )

    async def _request_id_for_trace(
        request: Request, agent_id: str, trace_id: str
    ) -> str:
        """Resolve a trace to the request it belongs to.

        Trace and request are one-to-one, so the admin UI addresses requests by
        the trace it already shows and never has to carry a request id.
        """
        resolved_agent_id = require_agent_id(agent_id)
        resolved_trace_id = require_trace_id(trace_id)
        store = get_runtime_store(request, resolved_agent_id)
        request_id = await store.get_request_id_for_trace(
            resolved_trace_id, agent_id=resolved_agent_id
        )
        if not request_id:
            raise APIError(
                code="REQUEST_NOT_FOUND",
                message=f"trace '{resolved_trace_id}' has no request",
                status_code=404,
            )
        return request_id

    @router.get("/agent/{agent_id}/trace/{trace_id}/status")
    async def get_trace_request_status(
        request: Request, agent_id: str, trace_id: str
    ) -> dict[str, Any]:
        request_id = await _request_id_for_trace(request, agent_id, trace_id)
        client = get_client(request, agent_id)
        try:
            status = await client.get_request_status(request_id)
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(_without_request_ids(status, trace_id))

    @router.get("/agent/{agent_id}/session/{session_id}/trace/{trace_id}/reasoning")
    async def get_reasoning_trace(
        request: Request,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        resolved_agent_id = require_agent_id(agent_id)
        resolved_session_id = require_session_id(session_id)
        resolved_trace_id = require_trace_id(trace_id)
        store = get_runtime_store(request, resolved_agent_id)
        events = await store.list_events(
            agent_id=resolved_agent_id,
            session_id=resolved_session_id,
            trace_id=resolved_trace_id,
        )
        trace_payload = build_reasoning_trace(events)
        return success(
            {
                "source": telemetry_event_source(),
                "agent_id": resolved_agent_id,
                "session_id": resolved_session_id,
                "trace_id": resolved_trace_id,
                **trace_payload,
            }
        )

    return router

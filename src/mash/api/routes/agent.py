"""Agent and session API routes."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from mash.runtime import AgentClientError
from mash.runtime.client import (
    AgentUnavailableError,
    InteractionClosedError,
    RequestModeConflictError,
)
from mash.runtime.errors import RequestStaleError
from mash.runtime.host.host import AgentDisabledError, AgentInUseError
from mash.runtime.host.processes import AgentStartError
from mash.runtime.events import build_reasoning_trace
from mash.memory.signals import build_default_signal_collector
from mash.runtime import sessions
from mash.runtime.structured_output import serialize_structured_output

from .common import (
    APIError,
    CompactSessionRequest,
    RequestModeValue,
    SetRequestModeRequest,
    SubmitRequest,
    build_runtime_event_sse_payload,
    get_client,
    require_user_id,
    require_agent_id,
    require_message,
    require_session_id,
    require_trace_id,
    state_from_request,
    success,
    telemetry_event_source,
)
from .common import (
    get_runtime_store,
)


logger = logging.getLogger(__name__)

_REQUEST_ID_KEYS = ("request_id", "workflow_id")


async def _cancel_with_children(
    request: Request, agent_id: str, request_id: str
) -> dict[str, Any]:
    """Cancel a request through its agent, then cancel its running children.

    Children are the requests its RunSubAgents calls started. Each is
    cancelled through the client for its own agent. Splitting is one level
    deep, so there is no further level to walk. A child whose agent is
    disabled is skipped, and so is one that fails to cancel: the parent is
    already cancelled.
    """
    client = get_client(request, agent_id)
    try:
        result = await client.cancel_request(request_id)
    except AgentUnavailableError:
        raise
    except (KeyError, AgentClientError) as exc:
        raise APIError(
            code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
        ) from exc
    pool = state_from_request(request).pool
    store = pool.get_runtime_store()
    if store is None:
        return result
    for child in await store.list_child_requests(request_id, status="running"):
        child_agent_id = child.get("agent_id")
        if not child_agent_id:
            continue
        try:
            await pool.get_client(child_agent_id).cancel_request(child["request_id"])
        except (AgentDisabledError, ValueError):
            continue
        except AgentClientError as exc:
            logger.warning(
                "could not cancel child request %s of %s: %s",
                child["request_id"],
                request_id,
                exc,
            )
    return result


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
        state = state_from_request(request)
        described = _described_agent(request, agent_id)
        if not state.pool.is_agent_enabled(agent_id):
            return success({"agent": described, "session": None})
        client = get_client(request, agent_id)
        health_payload = await client.health()
        session_payload = (
            health_payload.get("session") if isinstance(health_payload, dict) else {}
        )
        return success({"agent": described, "session": session_payload})

    @router.post("/agent/{agent_id}/request")
    async def submit_request(
        request: Request, agent_id: str, body: SubmitRequest
    ) -> dict[str, Any]:
        client = get_client(request, agent_id)
        try:
            structured_output = serialize_structured_output(body.structured_output)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="INVALID_STRUCTURED_OUTPUT",
                message=str(exc),
                status_code=400,
            ) from exc
        request_id = await client.post_request(
            require_message(body.message),
            session_id=require_session_id(body.session_id),
            structured_output=structured_output,
            metadata=body.metadata,
            user_id=require_user_id(body.user_id),
            mode=body.mode,
            traceparent=request.headers.get("traceparent"),
            tracestate=request.headers.get("tracestate"),
        )
        return success({"request_id": request_id})

    @router.post("/agent/{agent_id}/request/{request_id}/mode")
    async def set_request_mode(
        request: Request,
        agent_id: str,
        request_id: str,
        body: SetRequestModeRequest,
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
            result = await client.set_request_mode(normalized, body.mode)
        except RequestModeConflictError as exc:
            raise APIError(
                code="REQUEST_TERMINAL", message=str(exc), status_code=409
            ) from exc
        except AgentUnavailableError:
            raise
        except AgentClientError as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(result)

    @router.get("/agent/{agent_id}/request/{request_id}/events")
    async def stream_request_events(
        request: Request,
        agent_id: str,
        request_id: str,
    ) -> StreamingResponse:
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
                async for event in client.stream_response(normalized_request_id):
                    event_name = str(event.get("event") or "message")
                    payload = event.get("data")
                    yield build_runtime_event_sse_payload(event_name, payload)
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

    @router.post("/agent/{agent_id}/request/{request_id}/interaction")
    async def post_interaction(
        request: Request, agent_id: str, request_id: str
    ) -> dict[str, Any]:
        client = get_client(request, agent_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise APIError(
                code="INVALID_REQUEST",
                message="Request body must be an object",
                status_code=400,
            )
        interaction_id = body.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="interaction_id is required",
                status_code=400,
            )
        response_value = body.get("response")
        try:
            result = await client.post_interaction(
                request_id.strip(),
                interaction_id=interaction_id.strip(),
                response=response_value,
            )
        except InteractionClosedError as exc:
            raise APIError(
                code="INTERACTION_CLOSED", message=str(exc), status_code=409
            ) from exc
        except AgentUnavailableError:
            raise
        except AgentClientError as exc:
            raise APIError(
                code="INTERACTION_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(result)

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

    @router.post("/agent/{agent_id}/request/{request_id}/resume")
    async def resume_request(
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
            result = await client.resume_request(normalized)
        except RequestStaleError as exc:
            raise APIError(
                code="REQUEST_STALE", message=str(exc), status_code=409
            ) from exc
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(result)

    @router.post("/agent/{agent_id}/request/{request_id}/cancel")
    async def cancel_request(
        request: Request, agent_id: str, request_id: str
    ) -> dict[str, Any]:
        normalized = request_id.strip()
        if not normalized:
            raise APIError(
                code="INVALID_REQUEST",
                message="request_id is required",
                status_code=400,
            )
        return success(await _cancel_with_children(request, agent_id, normalized))

    @router.post("/agent/{agent_id}/request/{request_id}/rerun")
    async def rerun_request(
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
            result = await client.rerun_request(normalized)
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(result)

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
    async def list_sessions(request: Request, agent_id: str) -> dict[str, Any]:
        resolved_agent_id, store = _session_store(request, agent_id)
        return success(
            {"sessions": await sessions.list_sessions(store, app_id=resolved_agent_id)}
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
                app_id=resolved_agent_id,
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
        mode: Optional[RequestModeValue] = Query(default=None),
        status: Optional[
            Literal["running", "completed", "failed", "cancelled"]
        ] = Query(default=None),
        started_by: Optional[str] = Query(default=None),
    ) -> dict[str, Any]:
        """The session's requests, newest first, from every agent in it."""
        store = get_runtime_store(request, require_agent_id(agent_id))
        records = await store.list_session_requests(
            require_session_id(session_id),
            mode=mode,
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
                            "message",
                            "mode",
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
                    store, app_id=resolved_agent_id, session_id=session_id, limit=limit
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
                    app_id=resolved_agent_id,
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
            resolved_trace_id, app_id=resolved_agent_id
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

    @router.post("/agent/{agent_id}/trace/{trace_id}/cancel")
    async def cancel_trace_request(
        request: Request, agent_id: str, trace_id: str
    ) -> dict[str, Any]:
        request_id = await _request_id_for_trace(request, agent_id, trace_id)
        result = await _cancel_with_children(request, agent_id, request_id)
        return success(_without_request_ids(result, trace_id))

    @router.post("/agent/{agent_id}/trace/{trace_id}/resume")
    async def resume_trace_request(
        request: Request, agent_id: str, trace_id: str
    ) -> dict[str, Any]:
        request_id = await _request_id_for_trace(request, agent_id, trace_id)
        client = get_client(request, agent_id)
        try:
            result = await client.resume_request(request_id)
        except RequestStaleError as exc:
            raise APIError(
                code="REQUEST_STALE", message=str(exc), status_code=409
            ) from exc
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(_without_request_ids(result, trace_id))

    @router.post("/agent/{agent_id}/trace/{trace_id}/rerun")
    async def rerun_trace_request(
        request: Request, agent_id: str, trace_id: str
    ) -> dict[str, Any]:
        """Start the trace's request over as a new request.

        The new trace does not exist until the new request starts executing, so
        this reports acceptance only; the caller reloads its trace list and the
        rerun appears as a new row.
        """
        request_id = await _request_id_for_trace(request, agent_id, trace_id)
        client = get_client(request, agent_id)
        try:
            result = await client.rerun_request(request_id)
        except AgentUnavailableError:
            raise
        except (KeyError, AgentClientError) as exc:
            raise APIError(
                code="REQUEST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(
            {
                "rerun_of_trace_id": trace_id,
                "agent_id": result.get("agent_id"),
                "session_id": result.get("session_id"),
                "status": result.get("status") or "accepted",
            }
        )

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
            app_id=resolved_agent_id,
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

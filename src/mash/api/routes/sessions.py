"""Events API: every input to a host is an event in a session.

``POST /sessions/{session_id}/events`` takes a new event, or, with an
``interaction_id``, an answer to the open interaction of an existing one.
The other routes read an event, its stream and the ledgers, and cancel.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mash.runtime.client import (
    AgentClientError,
    AgentUnavailableError,
    InteractionClosedError,
    InvalidInteractionResponseError,
)
from mash.runtime.structured_output import serialize_structured_output
from mash.sessions import (
    REPORTED_STATUSES,
    EventNotFoundError,
    EventService,
    EventTerminalError,
    NewEvent,
    SessionMismatchError,
    validate_event_id,
    validate_session_id,
)

from .browser import view_url
from .common import (
    APIError,
    build_runtime_event_sse_payload,
    require_app_id,
    require_policy_id,
    state_from_request,
    success,
)

_NEW_EVENT_ONLY = (
    "host_id", "app_id", "policy_id", "message", "context", "metadata", "structured_output",
)
_ANSWER_KEYS = {"event_id", "interaction_id", "response"}
EVENTS_DEFAULT_LIMIT = 100
EVENTS_MAX_LIMIT = 500


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def _service(request: Request) -> EventService:
    service = state_from_request(request).pool.get_event_service()
    if service is None:
        raise APIError(
            code="EVENTS_UNAVAILABLE", message="events are not available", status_code=503
        )
    return service


def _session_id(value: str) -> str:
    try:
        return validate_session_id(value)
    except ValueError as exc:
        raise _invalid(str(exc)) from exc


def _event_id(value: Any) -> str:
    try:
        return validate_event_id(value)
    except ValueError as exc:
        raise _invalid(str(exc)) from exc


def _not_found(exc: EventNotFoundError) -> APIError:
    return APIError(code="EVENT_NOT_FOUND", message=str(exc), status_code=404)


def _statuses(value: Optional[str]) -> set[str] | None:
    if value is None or not value.strip():
        return None
    statuses = {part.strip() for part in value.split(",") if part.strip()}
    unknown = statuses - REPORTED_STATUSES
    if unknown:
        raise _invalid(
            f"unknown status {sorted(unknown)}; use {', '.join(sorted(REPORTED_STATUSES))}"
        )
    return statuses


def _limit(value: Optional[int]) -> int:
    if value is None:
        return EVENTS_DEFAULT_LIMIT
    if value < 1:
        raise _invalid("limit must be at least 1")
    return min(int(value), EVENTS_MAX_LIMIT)


def _new_event(body: dict[str, Any], event_id: str) -> NewEvent:
    for key in ("host_id", "message"):
        if not isinstance(body.get(key), str) or not body[key].strip():
            raise _invalid(f"{key} is required")
    for key in ("app_id", "policy_id"):
        if key not in body:
            raise _invalid(f"{key} is required")
    context = body.get("context")
    if context is not None and not isinstance(context, str):
        raise _invalid("context must be a string")
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise _invalid("metadata must be an object")
    structured_output = body.get("structured_output")
    if structured_output is not None:
        try:
            structured_output = serialize_structured_output(structured_output)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="INVALID_STRUCTURED_OUTPUT", message=str(exc), status_code=400
            ) from exc
    unknown = set(body) - {"event_id", *_NEW_EVENT_ONLY}
    if unknown:
        raise _invalid(f"unknown fields: {', '.join(sorted(unknown))}")
    return NewEvent(
        event_id=event_id,
        host_id=body["host_id"].strip(),
        app_id=require_app_id(body["app_id"]),
        policy_id=require_policy_id(body["policy_id"]),
        message=body["message"],
        context=context,
        metadata=metadata,
        structured_output=structured_output,
    )


def build_sessions_router() -> APIRouter:
    router = APIRouter()

    @router.post("/sessions/{session_id}/events")
    async def post_event(request: Request, session_id: str) -> Any:
        """A new event, or an answer to an interaction of an existing one."""
        service = _service(request)
        resolved_session = _session_id(session_id)
        try:
            body = await request.json()
        except ValueError as exc:
            raise _invalid("Request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise _invalid("Request body must be an object")
        event_id = _event_id(body.get("event_id"))

        if "interaction_id" in body:
            return await _answer(service, resolved_session, event_id, body)

        # A seen event returns its row, whatever the body.
        existing = await service.store.get_event(resolved_session, event_id)
        if existing is not None:
            return JSONResponse(success(await service.describe(existing)), status_code=200)
        new = _new_event(body, event_id)
        try:
            record, created = await service.post_event(resolved_session, new)
        except ValueError as exc:
            raise APIError(code="HOST_NOT_FOUND", message=str(exc), status_code=404) from exc
        except SessionMismatchError as exc:
            raise APIError(
                code=f"SESSION_{exc.kind.upper()}_MISMATCH", message=str(exc), status_code=409
            ) from exc
        return JSONResponse(
            success(await service.describe(record)), status_code=202 if created else 200
        )

    async def _answer(
        service: EventService, session_id: str, event_id: str, body: dict[str, Any]
    ) -> Any:
        mixed = sorted(set(body) - _ANSWER_KEYS)
        if mixed:
            raise _invalid(
                f"an answer takes event_id, interaction_id and response, not {', '.join(mixed)}"
            )
        interaction_id = body.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            raise _invalid("interaction_id is required")
        if "response" not in body:
            raise _invalid("response is required")
        try:
            record = await service.answer(
                session_id, event_id, interaction_id.strip(), body["response"]
            )
        except EventNotFoundError as exc:
            raise _not_found(exc) from exc
        except KeyError as exc:
            raise APIError(
                code="INTERACTION_NOT_FOUND",
                message=f"interaction '{interaction_id}' not found",
                status_code=404,
            ) from exc
        except InvalidInteractionResponseError as exc:
            raise APIError(
                code="INVALID_INTERACTION_RESPONSE", message=str(exc), status_code=400
            ) from exc
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
        state = await service.describe(record)
        return success(
            {
                "event_id": event_id,
                "request_id": state["request_id"],
                "status": state["status"],
                "interaction_id": interaction_id.strip(),
            }
        )

    @router.get("/sessions/{session_id}/events/{event_id}")
    async def get_event(request: Request, session_id: str, event_id: str) -> dict[str, Any]:
        service = _service(request)
        record = await service.store.get_event(_session_id(session_id), _event_id(event_id))
        if record is None:
            raise _not_found(EventNotFoundError(session_id, event_id))
        return success(await service.describe(record))

    @router.get("/sessions/{session_id}/events")
    async def list_session_events(
        request: Request,
        session_id: str,
        status: Optional[str] = Query(default=None),
        after_seq: int = Query(default=0, ge=0),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        events = await _service(request).list_events(
            session_id=_session_id(session_id),
            statuses=_statuses(status),
            after_seq=after_seq,
            limit=_limit(limit),
        )
        return success({"events": events})

    @router.get("/hosts/{host_id}/events")
    async def list_host_events(
        request: Request,
        host_id: str,
        status: Optional[str] = Query(default=None),
        after_seq: int = Query(default=0, ge=0),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        events = await _service(request).list_events(
            host_id=host_id.strip(),
            statuses=_statuses(status),
            after_seq=after_seq,
            limit=_limit(limit),
        )
        return success({"events": events})

    @router.post("/sessions/{session_id}/events/{event_id}/cancel")
    async def cancel_event(request: Request, session_id: str, event_id: str) -> dict[str, Any]:
        service = _service(request)
        try:
            record = await service.cancel(_session_id(session_id), _event_id(event_id))
        except EventNotFoundError as exc:
            raise _not_found(exc) from exc
        except EventTerminalError as exc:
            raise APIError(code="EVENT_TERMINAL", message=str(exc), status_code=409) from exc
        return success(await service.describe(record))

    @router.post("/sessions/{session_id}/events/{event_id}/interactions/{interaction_id}/handoff")
    async def mint_handoff_link(
        request: Request, session_id: str, event_id: str, interaction_id: str
    ) -> dict[str, Any]:
        """A new live-view link for an open ``browser`` interaction of the event.

        It lasts ``MASH_BROWSER_HANDOFF_SECONDS``; earlier links keep working
        until they expire or the interaction closes. A relayed interaction's
        link opens the asking child's tab.
        """
        service = _service(request)
        state = state_from_request(request)
        browsers = state.pool.get_browsers()
        if browsers is None or not state.host_secret:
            raise APIError(
                code="BROWSER_UNAVAILABLE", message="this host has no browsers", status_code=503
            )
        try:
            policy_id, request_id = await service.handoff_target(
                _session_id(session_id), _event_id(event_id), interaction_id.strip()
            )
        except EventNotFoundError as exc:
            raise _not_found(exc) from exc
        except KeyError as exc:
            raise APIError(
                code="INTERACTION_NOT_FOUND",
                message=f"no open browser interaction '{interaction_id}'",
                status_code=404,
            ) from exc
        handoff = browsers.mint_link(policy_id, request_id, interaction_id.strip())
        with contextlib.suppress(Exception):
            await browsers.front(policy_id, request_id)
        return success(
            {"url": view_url(state.public_url, handoff.token), "expires_at": handoff.expires_at}
        )

    @router.get("/sessions/{session_id}/events/{event_id}/stream")
    async def stream_event(
        request: Request,
        session_id: str,
        event_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        """The event's request as SSE, each frame with its seq as ``id:``.

        While the event is queued the stream holds with an ``event.queued``
        frame and keepalives, then relays the request from its first frame.
        ``after`` is the seq of the last frame the reader has.
        """
        service = _service(request)
        resolved_session, resolved_event = _session_id(session_id), _event_id(event_id)
        if await service.store.get_event(resolved_session, resolved_event) is None:
            raise _not_found(EventNotFoundError(resolved_session, resolved_event))

        async def _generate() -> AsyncIterator[str]:
            try:
                async for name, data, seq in service.stream(
                    resolved_session, resolved_event, after=after
                ):
                    if name is None:
                        yield ": keepalive\n\n"
                    else:
                        yield build_runtime_event_sse_payload(name, data, seq=seq)
            except AgentClientError as exc:
                yield build_runtime_event_sse_payload(
                    "request.error",
                    {"session_id": resolved_session, "event_id": resolved_event, "error": str(exc)},
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return router

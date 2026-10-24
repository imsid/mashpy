"""H2A relay for calls agent computers make to the host.

A primary agent on its computer calls its subagents through these routes,
never directly. They speak H2A as ``AgentServer`` does (no ``{data}``
envelope), so the computer uses a plain ``AgentClient`` pointed at
``{host}/h2a``. Each call authenticates with the caller's agent token; the
host checks it against the request's ``parent`` and host snapshot, then
forwards to the subagent's client. The subagent request acts for the parent
request's user: the host copies ``user_id`` from the parent's stored request,
so a primary cannot name another user.
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mash.runtime.client import AgentClientError, AgentUnavailableError
from mash.runtime.events import RuntimeEventType
from mash.runtime.host.tokens import agent_for_token
from mash.runtime.requests import parent_request_metadata, validate_request_id

from .common import build_runtime_event_sse_payload, state_from_request


def h2a_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def build_h2a_router() -> APIRouter:
    router = APIRouter(prefix="/h2a")

    def _caller(request: Request) -> str | JSONResponse:
        state = state_from_request(request)
        if not state.host_secret:
            return h2a_error(503, "H2A_UNAVAILABLE", "host secret is not configured")
        caller = agent_for_token(
            state.host_secret, bearer_token(request), state.pool.list_agents()
        )
        if caller is None:
            return h2a_error(401, "UNAUTHORIZED", "Missing or invalid agent token")
        return caller

    @router.post("/agent/{agent_id}/request")
    async def submit_subagent_request(request: Request, agent_id: str) -> Response:
        caller = _caller(request)
        if isinstance(caller, JSONResponse):
            return caller
        try:
            body = await request.json()
        except ValueError:
            return h2a_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return h2a_error(400, "INVALID_REQUEST", "Request body must be an object")

        parent = body.get("parent")
        try:
            parent_request_metadata(parent, agent_id=agent_id)
        except ValueError as exc:
            return h2a_error(400, "INVALID_PARENT", str(exc))
        if parent.get("kind") != "subagent" or parent.get("agent_id") != caller:
            return h2a_error(
                403, "FORBIDDEN", f"agent '{caller}' may only submit its own subagent calls"
            )
        host = body.get("host")
        subagents = host.get("subagents") if isinstance(host, dict) else None
        if not isinstance(subagents, list) or agent_id not in subagents:
            return h2a_error(
                403, "FORBIDDEN", f"subagent '{agent_id}' is not in the request's host"
            )

        state = state_from_request(request)
        store = state.pool.get_runtime_store()
        if store is None:
            return h2a_error(503, "H2A_UNAVAILABLE", "runtime store is not available")
        identity = await store.get_request_identity(str(parent.get("request_id")))
        if identity is None:
            return h2a_error(400, "INVALID_PARENT", "parent request not found")
        if identity.agent_id != caller:
            return h2a_error(
                403, "FORBIDDEN", f"parent request does not belong to agent '{caller}'"
            )
        if "user_id" in body and body.get("user_id") != identity.user_id:
            return h2a_error(
                403, "FORBIDDEN", "user_id must match the parent request's user"
            )

        message = body.get("message")
        session_id = body.get("session_id")
        if not isinstance(message, str) or not message.strip():
            return h2a_error(400, "INVALID_REQUEST", "message is required")
        if not isinstance(session_id, str) or not session_id.strip():
            return h2a_error(400, "INVALID_REQUEST", "session_id is required")
        request_id = body.get("request_id")
        if request_id is not None:
            try:
                validate_request_id(request_id)
            except ValueError as exc:
                return h2a_error(400, "INVALID_REQUEST", str(exc))

        try:
            client = state.pool.get_client(agent_id)
        except ValueError as exc:
            return h2a_error(404, "AGENT_NOT_FOUND", str(exc))
        try:
            submitted_id = await client.post_request(
                message,
                session_id=session_id.strip(),
                structured_output=body.get("structured_output"),
                metadata=body.get("metadata"),
                request_id=request_id,
                context=body.get("context"),
                host=host,
                parent=parent,
                user_id=identity.user_id,
            )
        except AgentUnavailableError as exc:
            return h2a_error(503, "AGENT_UNAVAILABLE", str(exc))
        except AgentClientError as exc:
            return h2a_error(502, "AGENT_ERROR", str(exc))
        return JSONResponse(
            {
                "request_id": submitted_id,
                "agent_id": agent_id,
                "session_id": session_id.strip(),
                "status": "accepted",
            },
            status_code=202,
        )

    @router.get("/agent/{agent_id}/request/{request_id}")
    async def stream_subagent_request(
        request: Request, agent_id: str, request_id: str
    ) -> Response:
        caller = _caller(request)
        if isinstance(caller, JSONResponse):
            return caller
        state = state_from_request(request)
        store = state.pool.get_runtime_store()
        events = await store.list_request_events(request_id) if store is not None else []
        accepted = next(
            (e for e in events if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value),
            None,
        )
        if accepted is None or accepted.agent_id != agent_id:
            return h2a_error(404, "REQUEST_NOT_FOUND", "Request not found")
        parent = dict((accepted.payload or {}).get("request_metadata") or {}).get("parent")
        if not isinstance(parent, dict) or parent.get("agent_id") != caller:
            return h2a_error(
                403, "FORBIDDEN", f"agent '{caller}' did not submit request '{request_id}'"
            )

        client = state.pool.get_client(agent_id)

        async def _generate() -> AsyncIterator[str]:
            async for event in client.stream_response(request_id):
                yield build_runtime_event_sse_payload(
                    str(event.get("event") or ""), event.get("data")
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return router


__all__ = ["bearer_token", "build_h2a_router", "h2a_error"]

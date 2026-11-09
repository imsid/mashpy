"""H2A relay for calls agent processes make to the host.

The companion, in its process, starts its RunSubAgents children through these
routes, never directly, and starts and reads its RunWorkflow runs here. They
speak H2A as ``AgentServer`` does (no ``{data}`` envelope), so the agent uses a
plain ``AgentClient`` pointed at ``{host}/h2a``. Each call authenticates with
the caller's agent token; the host checks it against the request's ``parent``
and the host snapshot stored with the parent request, not the caller's body:
a child runs only on one of the host's agents and acts for the parent
request's user, and a workflow runs only when the host lists it. So a caller
can neither widen its host nor name another user.
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mash.runtime.client import (
    AgentClientError,
    AgentUnavailableError,
    SessionUserConflictError,
)
from mash.runtime.events import RuntimeEventType
from mash.runtime.host.host import AgentDisabledError
from mash.runtime.host.tokens import agent_for_token
from mash.runtime.requests import parent_request_metadata, validate_request_id
from mash.workflows.service import (
    DuplicateWorkflowRunError,
    WorkflowInputValidationError,
    WorkflowNotFoundError,
)

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


async def _stored_host(store, request_id: str) -> dict | None:
    """The host snapshot stored with a request's accepted event, or None."""
    events = await store.list_request_events(request_id)
    accepted = next(
        (e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value),
        None,
    )
    if accepted is None:
        return None
    host = dict((accepted.payload or {}).get("request_metadata") or {}).get("host")
    return dict(host) if isinstance(host, dict) else None


async def _check_continued_session(
    pool, store, *, session_id: str, agent_id: str, caller_session_id: str
) -> JSONResponse | None:
    """None when a child of ``agent_id`` may continue ``session_id``.

    The session must have no requests yet, or only requests of ``agent_id``
    whose turns carry the caller's session as ``primary_session_id``.
    """
    requests = await store.list_session_requests(session_id)
    if any(record.get("agent_id") != agent_id for record in requests):
        return h2a_error(
            403, "FORBIDDEN", f"session '{session_id}' is not a session of '{agent_id}'"
        )
    if not requests:
        return None
    memory = pool.get_memory_store(agent_id)
    if memory is None:
        return h2a_error(
            403, "FORBIDDEN", f"sessions of '{agent_id}' cannot be continued"
        )
    turns = await memory.get_turns(session_id=session_id, app_id=agent_id)
    for turn in turns:
        metadata = turn.get("metadata") or {}
        if (
            metadata.get("subagent_id") != agent_id
            or metadata.get("primary_session_id") != caller_session_id
        ):
            return h2a_error(
                403,
                "FORBIDDEN",
                f"session '{session_id}' was not started from the caller's session",
            )
    return None


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

        state = state_from_request(request)
        store = state.pool.get_runtime_store()
        if store is None:
            return h2a_error(503, "H2A_UNAVAILABLE", "runtime store is not available")
        parent_request_id = str(parent.get("request_id"))
        identity = await store.get_request_identity(parent_request_id)
        if identity is None:
            return h2a_error(400, "INVALID_PARENT", "parent request not found")
        if identity.agent_id != caller:
            return h2a_error(
                403, "FORBIDDEN", f"parent request does not belong to agent '{caller}'"
            )
        # The composition the parent was submitted with, as stored; the body's
        # "host" is the caller's copy and is not trusted.
        host = await _stored_host(store, parent_request_id)
        agents = host.get("agents") if host is not None else None
        if not isinstance(agents, list) or agent_id not in agents:
            return h2a_error(
                403, "FORBIDDEN", f"agent '{agent_id}' is not in the request's host"
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
        # A session other than the child's own new one continues earlier work:
        # it must be one this agent ran for children of the caller's session.
        if session_id.strip() != request_id:
            refused = await _check_continued_session(
                state.pool,
                store,
                session_id=session_id.strip(),
                agent_id=agent_id,
                caller_session_id=str(parent.get("session_id")),
            )
            if refused is not None:
                return refused

        try:
            client = state.pool.get_client(agent_id)
        except AgentDisabledError as exc:
            return h2a_error(409, "AGENT_DISABLED", str(exc))
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
                mode=body.get("mode"),
            )
        except AgentUnavailableError as exc:
            return h2a_error(503, "AGENT_UNAVAILABLE", str(exc))
        except SessionUserConflictError as exc:
            return h2a_error(409, "SESSION_USER_MISMATCH", str(exc))
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

    async def _own_child(
        request: Request, agent_id: str, request_id: str
    ) -> JSONResponse | None:
        """None when the caller submitted ``request_id`` on ``agent_id``."""
        caller = _caller(request)
        if isinstance(caller, JSONResponse):
            return caller
        state = state_from_request(request)
        store = state.pool.get_runtime_store()
        events = await store.list_request_events(request_id) if store is not None else []
        accepted = next(
            (e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value),
            None,
        )
        if accepted is None or accepted.agent_id != agent_id:
            return h2a_error(404, "REQUEST_NOT_FOUND", "Request not found")
        parent = dict((accepted.payload or {}).get("request_metadata") or {}).get("parent")
        if not isinstance(parent, dict) or parent.get("agent_id") != caller:
            return h2a_error(
                403, "FORBIDDEN", f"agent '{caller}' did not submit request '{request_id}'"
            )
        return None

    @router.post("/agent/{agent_id}/request/{request_id}/cancel")
    async def cancel_subagent_request(
        request: Request, agent_id: str, request_id: str
    ) -> Response:
        refused = await _own_child(request, agent_id, request_id)
        if refused is not None:
            return refused
        try:
            client = state_from_request(request).pool.get_client(agent_id)
        except AgentDisabledError as exc:
            return h2a_error(409, "AGENT_DISABLED", str(exc))
        try:
            result = await client.cancel_request(request_id)
        except AgentUnavailableError as exc:
            return h2a_error(503, "AGENT_UNAVAILABLE", str(exc))
        except AgentClientError as exc:
            return h2a_error(502, "AGENT_ERROR", str(exc))
        return JSONResponse(result)

    @router.get("/agent/{agent_id}/request/{request_id}")
    async def stream_subagent_request(
        request: Request, agent_id: str, request_id: str
    ) -> Response:
        refused = await _own_child(request, agent_id, request_id)
        if refused is not None:
            return refused
        client = state_from_request(request).pool.get_client(agent_id)

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

    async def _own_parent(
        request: Request, parent_request_id: object
    ) -> tuple[str, object, object] | JSONResponse:
        """The caller, the store and the parent's identity, or a refusal."""
        caller = _caller(request)
        if isinstance(caller, JSONResponse):
            return caller
        if not isinstance(parent_request_id, str) or not parent_request_id.strip():
            return h2a_error(400, "INVALID_PARENT", "parent request_id is required")
        store = state_from_request(request).pool.get_runtime_store()
        if store is None:
            return h2a_error(503, "H2A_UNAVAILABLE", "runtime store is not available")
        identity = await store.get_request_identity(parent_request_id)
        if identity is None:
            return h2a_error(400, "INVALID_PARENT", "parent request not found")
        if identity.agent_id != caller:
            return h2a_error(
                403, "FORBIDDEN", f"parent request does not belong to agent '{caller}'"
            )
        return caller, store, identity

    @router.post("/workflow/{workflow_id}/run")
    async def run_workflow(request: Request, workflow_id: str) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return h2a_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return h2a_error(400, "INVALID_REQUEST", "Request body must be an object")
        parent = body.get("parent")
        parent_request_id = parent.get("request_id") if isinstance(parent, dict) else None
        owned = await _own_parent(request, parent_request_id)
        if isinstance(owned, JSONResponse):
            return owned
        _, store, identity = owned
        # The workflows the parent was submitted with, as stored.
        host = await _stored_host(store, parent_request_id)
        workflows = host.get("workflows") if host is not None else None
        if not isinstance(workflows, list) or workflow_id not in workflows:
            return h2a_error(
                403, "FORBIDDEN", f"workflow '{workflow_id}' is not in the request's host"
            )
        workflow_input = body.get("input")
        if workflow_input is None:
            workflow_input = {}
        if not isinstance(workflow_input, dict):
            return h2a_error(400, "INVALID_REQUEST", "input must be an object")
        dedup_key = body.get("dedup_key")
        # A key names a position in the parent request, so it cannot reach
        # another request's run.
        if dedup_key is not None and (
            not isinstance(dedup_key, str)
            or not dedup_key.startswith(f"{parent_request_id}-")
        ):
            return h2a_error(
                400, "INVALID_REQUEST", "dedup_key must begin with the parent request id"
            )
        service = state_from_request(request).pool.get_workflow_service()
        try:
            run = await service.run_workflow(
                workflow_id,
                dedup_key=dedup_key,
                workflow_input=workflow_input,
                user_id=identity.user_id,
            )
            status_code = 202
        except DuplicateWorkflowRunError as exc:
            run, status_code = exc.existing_run, 200
        except WorkflowInputValidationError as exc:
            return JSONResponse(
                {
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": str(exc),
                        "details": exc.errors,
                    }
                },
                status_code=422,
            )
        except WorkflowNotFoundError as exc:
            return h2a_error(404, "WORKFLOW_NOT_FOUND", str(exc))
        except ValueError as exc:
            return h2a_error(400, "INVALID_REQUEST", str(exc))
        return JSONResponse(_run_payload(run), status_code=status_code)

    @router.get("/workflow/{workflow_id}/runs/{run_id}")
    async def get_workflow_run(request: Request, workflow_id: str, run_id: str) -> Response:
        owned = await _own_parent(request, request.query_params.get("parent_request_id"))
        if isinstance(owned, JSONResponse):
            return owned
        _, _, identity = owned
        service = state_from_request(request).pool.get_workflow_service()
        try:
            run = await service.get_run(workflow_id, run_id)
        except (WorkflowNotFoundError, ValueError):
            return h2a_error(404, "RUN_NOT_FOUND", "Workflow run not found")
        if run.workflow_id != workflow_id:
            return h2a_error(404, "RUN_NOT_FOUND", "Workflow run not found")
        if run.user_id != identity.user_id:
            return h2a_error(
                403, "FORBIDDEN", "the run does not act for the parent request's user"
            )
        return JSONResponse(_run_payload(run))

    return router


def _run_payload(run) -> dict:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "status": run.status,
        "result": run.result,
        "error": run.error,
    }


__all__ = ["bearer_token", "build_h2a_router", "h2a_error"]

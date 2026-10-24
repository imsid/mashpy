"""Sandbox relay: where an agent's bash calls reach the user's sandbox.

``POST /sandbox/exec`` takes ``{request_id, command, timeout}`` with the
calling agent's token. The host looks up the request, checks it belongs to the
caller, and runs the command in the sandbox of the user the request acts for,
in the caller's folder. The agent never names a user.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from mash.runtime.host.tokens import agent_for_token
from mash.sandbox.daemon import BASH_DEFAULT_TIMEOUT
from mash.sandbox.manager import SandboxCapacityError
from mash.sandbox.provider import SandboxUnavailableError

from .common import state_from_request
from .h2a import bearer_token, h2a_error


def build_sandbox_router() -> APIRouter:
    router = APIRouter(prefix="/sandbox")

    @router.post("/exec")
    async def exec_command(request: Request) -> Response:
        state = state_from_request(request)
        if not state.host_secret:
            return h2a_error(503, "SANDBOX_UNAVAILABLE", "host secret is not configured")
        caller = agent_for_token(
            state.host_secret, bearer_token(request), state.pool.list_registered_agents()
        )
        if caller is None:
            return h2a_error(401, "UNAUTHORIZED", "Missing or invalid agent token")
        try:
            body = await request.json()
        except ValueError:
            return h2a_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return h2a_error(400, "INVALID_REQUEST", "Request body must be an object")
        request_id = body.get("request_id")
        command = body.get("command")
        timeout = body.get("timeout", BASH_DEFAULT_TIMEOUT)
        if not isinstance(request_id, str) or not request_id.strip():
            return h2a_error(400, "INVALID_REQUEST", "request_id is required")
        if not isinstance(command, str) or not command.strip():
            return h2a_error(400, "INVALID_REQUEST", "command is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return h2a_error(400, "INVALID_REQUEST", "timeout must be a positive number")

        sandboxes = state.pool.get_sandboxes()
        store = state.pool.get_runtime_store()
        if sandboxes is None or store is None:
            return h2a_error(503, "SANDBOX_UNAVAILABLE", "this host has no sandboxes")
        identity = await store.get_request_identity(request_id)
        if identity is None:
            return h2a_error(404, "REQUEST_NOT_FOUND", "Request not found")
        if identity.agent_id != caller:
            return h2a_error(
                403, "FORBIDDEN", f"request does not belong to agent '{caller}'"
            )
        if identity.user_id is None:
            return h2a_error(403, "FORBIDDEN", "request acts for no user")

        try:
            result = await sandboxes.exec(
                identity.user_id, agent_id=caller, command=command, timeout=float(timeout)
            )
        except SandboxCapacityError as exc:
            return h2a_error(503, "SANDBOX_CAPACITY", str(exc))
        except SandboxUnavailableError as exc:
            return h2a_error(503, "SANDBOX_UNAVAILABLE", str(exc))
        return JSONResponse(result)

    return router


__all__ = ["build_sandbox_router"]

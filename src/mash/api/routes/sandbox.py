"""Sandbox relay and links: how agents and users reach a user's sandbox.

The relay is for agent processes. Each call carries the calling agent's token
and the id of the request it serves. The host looks up the request, checks it
belongs to the caller, and acts in the sandbox of the user the request acts
for, in the caller's folder. The agent never names a user.

- ``POST /sandbox/exec`` takes ``{request_id, command, timeout, stdin?}`` and
  runs the command. The command sees ``MASH_FILES_URL`` and ``MASH_PORTS_URL``,
  the user's links to the caller's folder and to the sandbox's ports.
- ``PUT /sandbox/files?request_id=&path=`` writes the body to a file in the
  caller's folder; ``GET`` with the same query reads one.

Links are for users. ``/s/<link token>/files/<agent_id>/<path>`` serves a file
from an agent's folder and ``/s/<link token>/ports/<n>/<path>`` forwards HTTP
to a server on port ``n`` in the sandbox. The link token names the user
(``mash.sandbox.tokens``), so these routes take no API key. Responses carry a
``sandbox`` Content-Security-Policy, so a page the model wrote cannot act as
the host's origin.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mash.runtime.host.tokens import agent_for_token
from mash.sandbox.daemon import BASH_DEFAULT_TIMEOUT, resolve_workspace_path
from mash.sandbox.manager import SandboxCapacityError
from mash.sandbox.provider import SandboxUnavailableError
from mash.sandbox.tokens import derive_link_token, user_for_link_token

from .common import AppRuntimeState, state_from_request
from .h2a import bearer_token, h2a_error

FILES_URL_ENV = "MASH_FILES_URL"
PORTS_URL_ENV = "MASH_PORTS_URL"
# Scripts run, but the page gets an opaque origin: no cookies or storage of the
# host's, and no same-origin calls to its API.
LINK_CSP = "sandbox allow-scripts allow-forms allow-popups allow-downloads"
_PROXY_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Request headers a link forwards to a server in the sandbox.
_FORWARDED_HEADERS = ("accept", "accept-language", "content-type", "user-agent")


@dataclass(frozen=True)
class _Caller:
    state: AppRuntimeState
    agent_id: str
    user_id: str


def link_base(public_url: str, secret: str, user_id: str) -> str:
    """The base of every link into ``user_id``'s sandbox."""
    return f"{public_url.rstrip('/')}/s/{derive_link_token(secret, user_id)}"


def link_env(public_url: str, secret: str, user_id: str, agent_id: str) -> dict[str, str]:
    """The link variables a command of ``agent_id`` for ``user_id`` sees."""
    base = link_base(public_url, secret, user_id)
    return {FILES_URL_ENV: f"{base}/files/{agent_id}", PORTS_URL_ENV: f"{base}/ports"}


async def _caller(request: Request, request_id: object) -> _Caller | JSONResponse:
    """Who is calling, for which user; or the error response."""
    state = state_from_request(request)
    if not state.host_secret:
        return h2a_error(503, "SANDBOX_UNAVAILABLE", "host secret is not configured")
    caller = agent_for_token(
        state.host_secret, bearer_token(request), state.pool.list_registered_agents()
    )
    if caller is None:
        return h2a_error(401, "UNAUTHORIZED", "Missing or invalid agent token")
    if not isinstance(request_id, str) or not request_id.strip():
        return h2a_error(400, "INVALID_REQUEST", "request_id is required")
    store = state.pool.get_runtime_store()
    if state.pool.get_sandboxes() is None or store is None:
        return h2a_error(503, "SANDBOX_UNAVAILABLE", "this host has no sandboxes")
    identity = await store.get_request_identity(request_id)
    if identity is None:
        return h2a_error(404, "REQUEST_NOT_FOUND", "Request not found")
    if identity.agent_id != caller:
        return h2a_error(403, "FORBIDDEN", f"request does not belong to agent '{caller}'")
    if identity.user_id is None:
        return h2a_error(403, "FORBIDDEN", "request acts for no user")
    return _Caller(state=state, agent_id=caller, user_id=identity.user_id)


def _sandbox_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SandboxCapacityError):
        return h2a_error(503, "SANDBOX_CAPACITY", str(exc))
    return h2a_error(503, "SANDBOX_UNAVAILABLE", str(exc))


def _passthrough(response) -> Response:
    """The daemon's error response, as the relay's."""
    return Response(
        response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


def build_sandbox_router() -> APIRouter:
    router = APIRouter(prefix="/sandbox")

    @router.post("/exec")
    async def exec_command(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return h2a_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return h2a_error(400, "INVALID_REQUEST", "Request body must be an object")
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        command = body.get("command")
        timeout = body.get("timeout", BASH_DEFAULT_TIMEOUT)
        stdin = body.get("stdin")
        if not isinstance(command, str) or not command.strip():
            return h2a_error(400, "INVALID_REQUEST", "command is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            return h2a_error(400, "INVALID_REQUEST", "timeout must be a positive number")
        if stdin is not None and not isinstance(stdin, str):
            return h2a_error(400, "INVALID_REQUEST", "stdin must be a string")

        state = caller.state
        try:
            result = await state.pool.get_sandboxes().exec(
                caller.user_id,
                agent_id=caller.agent_id,
                command=command,
                timeout=float(timeout),
                stdin=stdin,
                env=link_env(
                    state.public_url, state.host_secret, caller.user_id, caller.agent_id
                ),
            )
        except (SandboxCapacityError, SandboxUnavailableError) as exc:
            return _sandbox_error(exc)
        return JSONResponse(result)

    @router.api_route("/files", methods=["GET", "PUT"])
    async def files(request: Request) -> Response:
        caller = await _caller(request, request.query_params.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        path = resolve_workspace_path(request.query_params.get("path", ""))
        if path is None:
            return h2a_error(
                400, "INVALID_REQUEST", "path must be relative and stay in the agent's folder"
            )
        sandboxes = caller.state.pool.get_sandboxes()
        try:
            if request.method == "PUT":
                response = await sandboxes.write_file(
                    caller.user_id,
                    agent_id=caller.agent_id,
                    path=path,
                    data=await request.body(),
                )
            else:
                response = await sandboxes.read_file(
                    caller.user_id, agent_id=caller.agent_id, path=path
                )
        except (SandboxCapacityError, SandboxUnavailableError) as exc:
            return _sandbox_error(exc)
        if response.status_code != 200 or request.method == "PUT":
            return _passthrough(response)
        return Response(response.content, media_type="application/octet-stream")

    return router


def build_link_router() -> APIRouter:
    router = APIRouter(prefix="/s/{token}")

    def _user(request: Request, token: str) -> str | Response:
        state = state_from_request(request)
        sandboxes = state.pool.get_sandboxes()
        if not state.host_secret or sandboxes is None:
            return _link_error(404, "this host has no sandboxes")
        user_id = user_for_link_token(state.host_secret, token)
        if user_id is None:
            return _link_error(404, "no such link")
        return user_id

    @router.get("/files/{agent_id}/{path:path}")
    async def file_link(request: Request, token: str, agent_id: str, path: str) -> Response:
        user_id = _user(request, token)
        if isinstance(user_id, Response):
            return user_id
        if not path or path.endswith("/"):
            path = f"{path}index.html"
        resolved = resolve_workspace_path(path)
        if resolved is None:
            return _link_error(404, "no such file")
        try:
            response = await state_from_request(request).pool.get_sandboxes().read_file(
                user_id, agent_id=agent_id, path=resolved
            )
        except (SandboxCapacityError, SandboxUnavailableError) as exc:
            return _link_error(503, str(exc))
        if response.status_code != 200:
            return _link_error(response.status_code, _daemon_message(response))
        media_type = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
        return _link_response(response.content, 200, media_type=media_type)

    @router.get("/ports/{port}")
    async def port_root(port: int) -> Response:
        # Relative URLs in the served page need the trailing slash.
        return RedirectResponse(f"{port}/", status_code=307)

    @router.api_route("/ports/{port}/{path:path}", methods=_PROXY_METHODS)
    async def port_link(request: Request, token: str, port: int, path: str) -> Response:
        user_id = _user(request, token)
        if isinstance(user_id, Response):
            return user_id
        headers = {
            name: request.headers[name]
            for name in _FORWARDED_HEADERS
            if name in request.headers
        }
        try:
            response = await state_from_request(request).pool.get_sandboxes().proxy(
                user_id,
                port=port,
                method=request.method,
                path=path,
                query=request.url.query,
                headers=headers,
                body=await request.body(),
            )
        except (SandboxCapacityError, SandboxUnavailableError) as exc:
            return _link_error(503, str(exc))
        return _link_response(
            response.content,
            response.status_code,
            media_type=response.headers.get("content-type"),
            location=response.headers.get("location"),
        )

    return router


def _daemon_message(response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text or f"status {response.status_code}"


def _link_error(status_code: int, message: str) -> Response:
    return _link_response(message.encode(), status_code, media_type="text/plain")


def _link_response(
    content: bytes,
    status_code: int,
    *,
    media_type: str | None,
    location: str | None = None,
) -> Response:
    headers = {"Content-Security-Policy": LINK_CSP, "X-Content-Type-Options": "nosniff"}
    if location:
        headers["Location"] = location
    return Response(content, status_code=status_code, media_type=media_type, headers=headers)


__all__ = [
    "FILES_URL_ENV",
    "LINK_CSP",
    "PORTS_URL_ENV",
    "build_link_router",
    "build_sandbox_router",
    "link_base",
    "link_env",
]

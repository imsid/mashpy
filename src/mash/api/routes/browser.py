"""Browser relay, live views and site data: how agents, users and apps reach a browser.

The relay is for agent processes. Each call carries the calling agent's token
and the id of the request it serves. The host looks up the request, checks it
belongs to the caller, and acts in the browser of the user the request acts
for, on the sites the caller's spec lists. The agent names neither.

- ``POST /browser/call`` takes ``{request_id, tool, arguments}`` and runs one
  relayed tool in the request's tab.
- ``POST /browser/save`` takes ``{request_id, download_id, path}`` and copies a
  download into the caller's sandbox folder.
- ``POST /browser/upload`` takes ``{request_id, target, element, path}`` and
  gives a file from that folder to the file chooser ``target`` opens.
- ``POST /browser/handoff`` takes ``{request_id}`` and returns a live-view link
  for the request's user, ``{url, expires_at, timeout_seconds}``;
  ``POST /browser/handoff/end`` revokes it and returns the page.

The live view is for users: ``/s/<view token>/browser/`` serves noVNC's client
and bridges its websocket to the user's browser. The view token names the
user and the request and lapses when the handoff ends or expires.

Apps read and clear a user's signed-in sites under
``/api/v1/browser/users/{user_id}`` (``build_browser_api_router``).
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import posixpath
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mash.browser.catalog import RELAYED_TOOLS
from mash.browser.manager import BrowserCapacityError
from mash.browser.provider import BrowserUnavailableError
from mash.browser.sites import check_sites
from mash.runtime.host.tokens import agent_for_token
from mash.sandbox.daemon import resolve_workspace_path
from mash.sandbox.manager import SandboxCapacityError
from mash.sandbox.provider import SandboxUnavailableError

from .common import APIError, AppRuntimeState, state_from_request, success
from .h2a import bearer_token, h2a_error

_BROWSER_ERRORS = (BrowserCapacityError, BrowserUnavailableError)
_SANDBOX_ERRORS = (SandboxCapacityError, SandboxUnavailableError)


@dataclass(frozen=True)
class _Caller:
    state: AppRuntimeState
    agent_id: str
    user_id: str
    sites: list[str]


async def _caller(request: Request, request_id: object) -> _Caller | JSONResponse:
    """Who is calling, for which user and sites; or the error response."""
    state = state_from_request(request)
    if not state.host_secret:
        return h2a_error(503, "BROWSER_UNAVAILABLE", "host secret is not configured")
    caller = agent_for_token(
        state.host_secret, bearer_token(request), state.pool.list_registered_agents()
    )
    if caller is None:
        return h2a_error(401, "UNAUTHORIZED", "Missing or invalid agent token")
    if not isinstance(request_id, str) or not request_id.strip():
        return h2a_error(400, "INVALID_REQUEST", "request_id is required")
    store = state.pool.get_runtime_store()
    if state.pool.get_browsers() is None or store is None:
        return h2a_error(503, "BROWSER_UNAVAILABLE", "this host has no browsers")
    spec = state.pool.get_registered_agent_spec(caller)
    sites = check_sites(spec.browser_sites()) if spec is not None else []
    if not sites:
        return h2a_error(403, "FORBIDDEN", f"agent '{caller}' lists no browser sites")
    identity = await store.get_request_identity(request_id)
    if identity is None:
        return h2a_error(404, "REQUEST_NOT_FOUND", "Request not found")
    if identity.agent_id != caller:
        return h2a_error(403, "FORBIDDEN", f"request does not belong to agent '{caller}'")
    if identity.user_id is None:
        return h2a_error(403, "FORBIDDEN", "request acts for no user")
    return _Caller(state=state, agent_id=caller, user_id=identity.user_id, sites=sites)


async def _body(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return h2a_error(400, "INVALID_JSON", "Request body must be valid JSON")
    if not isinstance(body, dict):
        return h2a_error(400, "INVALID_REQUEST", "Request body must be an object")
    return body


def _browser_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, BrowserCapacityError):
        return h2a_error(503, "BROWSER_CAPACITY", str(exc))
    return h2a_error(503, "BROWSER_UNAVAILABLE", str(exc))


def _result(content: str, *, is_error: bool = False) -> JSONResponse:
    return JSONResponse({"content": content, "images": [], "page_url": None, "is_error": is_error})


def view_url(public_url: str, token: str) -> str:
    return f"{public_url.rstrip('/')}/s/{token}/browser/"


def build_browser_router() -> APIRouter:
    router = APIRouter(prefix="/browser")

    @router.post("/call")
    async def call(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        tool = body.get("tool")
        if tool not in RELAYED_TOOLS:
            return h2a_error(400, "UNKNOWN_TOOL", f"'{tool}' is not a browser tool")
        arguments = body.get("arguments") or {}
        if not isinstance(arguments, dict):
            return h2a_error(400, "INVALID_REQUEST", "arguments must be an object")
        try:
            result = await caller.state.pool.get_browsers().call(
                caller.user_id,
                request_id=body["request_id"],
                tool=tool,
                arguments=arguments,
                sites=caller.sites,
            )
        except _BROWSER_ERRORS as exc:
            return _browser_error(exc)
        return JSONResponse(result)

    @router.post("/save")
    async def save(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        sandboxes = caller.state.pool.get_sandboxes()
        if sandboxes is None:
            return _result("Saving downloads needs the sandbox, and this host has none.", is_error=True)
        download_id = body.get("download_id")
        path = resolve_workspace_path(str(body.get("path") or ""))
        if not isinstance(download_id, str) or not download_id.strip():
            return _result("download_id is required", is_error=True)
        if path is None:
            return _result("path must be relative and stay in your folder", is_error=True)
        try:
            response = await caller.state.pool.get_browsers().download(caller.user_id, download_id)
        except _BROWSER_ERRORS as exc:
            return _browser_error(exc)
        if response.status_code != 200:
            return _result(
                f"No finished download '{download_id}'. Wait for it to finish, or check the id.",
                is_error=True,
            )
        try:
            written = await sandboxes.write_file(
                caller.user_id, agent_id=caller.agent_id, path=path, data=response.content
            )
        except _SANDBOX_ERRORS as exc:
            return _result(f"Sandbox error: {exc}", is_error=True)
        if written.status_code != 200:
            return _result(f"Sandbox error: {written.text}", is_error=True)
        name = response.headers.get("x-mash-file-name") or download_id
        return _result(f"Saved {name} ({len(response.content)} bytes) to {path} in your folder.")

    @router.post("/upload")
    async def upload(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        sandboxes = caller.state.pool.get_sandboxes()
        if sandboxes is None:
            return _result("Uploads come from the sandbox, and this host has none.", is_error=True)
        path = resolve_workspace_path(str(body.get("path") or ""))
        target = body.get("target")
        if path is None:
            return _result("path must be relative and stay in your folder", is_error=True)
        if not isinstance(target, str) or not target.strip():
            return _result("target is required", is_error=True)
        try:
            read = await sandboxes.read_file(caller.user_id, agent_id=caller.agent_id, path=path)
        except _SANDBOX_ERRORS as exc:
            return _result(f"Sandbox error: {exc}", is_error=True)
        if read.status_code != 200:
            return _result(f"No file at {path} in your folder.", is_error=True)
        try:
            result = await caller.state.pool.get_browsers().upload(
                caller.user_id,
                request_id=body["request_id"],
                name=posixpath.basename(path),
                data=read.content,
                target=target,
                element=str(body.get("element") or "file input"),
                sites=caller.sites,
            )
        except _BROWSER_ERRORS as exc:
            return _browser_error(exc)
        return JSONResponse(result)

    @router.post("/handoff")
    async def handoff(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        browsers = caller.state.pool.get_browsers()
        handoff = browsers.start_handoff(caller.user_id, body["request_id"])
        # Best effort: the view shows whatever tab is in front.
        with contextlib.suppress(*_BROWSER_ERRORS):
            await browsers.front(caller.user_id, body["request_id"])
        return JSONResponse(
            {
                "url": view_url(caller.state.public_url, handoff.token),
                "expires_at": handoff.expires_at,
                "timeout_seconds": browsers.handoff_seconds,
            }
        )

    @router.post("/handoff/end")
    async def end_handoff(request: Request) -> Response:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        caller = await _caller(request, body.get("request_id"))
        if isinstance(caller, JSONResponse):
            return caller
        browsers = caller.state.pool.get_browsers()
        browsers.end_handoff(body["request_id"])
        try:
            result = await browsers.call(
                caller.user_id,
                request_id=body["request_id"],
                tool="browser_snapshot",
                arguments={},
                sites=caller.sites,
            )
        except _BROWSER_ERRORS as exc:
            return _browser_error(exc)
        return JSONResponse(result)

    return router


def build_browser_link_router() -> APIRouter:
    router = APIRouter(prefix="/s/{token}/browser")

    def _handoff(request: Request | WebSocket, token: str):
        state = state_from_request(request)
        browsers = state.pool.get_browsers()
        if not state.host_secret or browsers is None:
            return None
        return browsers.handoff_for(token)

    @router.get("/")
    async def view_root(request: Request, token: str) -> Response:
        if _handoff(request, token) is None:
            return _view_error()
        state = state_from_request(request)
        prefix = urlsplit(state.public_url).path.strip("/")
        socket_path = "/".join(p for p in (prefix, "s", token, "browser", "websockify") if p)
        return RedirectResponse(
            f"vnc.html?autoconnect=true&resize=scale&reconnect=true&path={socket_path}",
            status_code=307,
        )

    @router.get("/{path:path}")
    async def view_file(request: Request, token: str, path: str) -> Response:
        handoff = _handoff(request, token)
        if handoff is None:
            return _view_error()
        try:
            response = await state_from_request(request).pool.get_browsers().view(
                handoff.user_id, path=path, query=request.url.query
            )
        except _BROWSER_ERRORS as exc:
            return Response(str(exc).encode(), status_code=503, media_type="text/plain")
        media_type = (
            response.headers.get("content-type")
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream"
        )
        return Response(
            response.content,
            status_code=response.status_code,
            media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.websocket("/websockify")
    async def view_socket(websocket: WebSocket, token: str) -> None:
        handoff = _handoff(websocket, token)
        if handoff is None:
            await websocket.close(code=4404)
            return
        browsers = state_from_request(websocket).pool.get_browsers()
        try:
            endpoint = await browsers.endpoint(handoff.user_id)
        except _BROWSER_ERRORS:
            await websocket.close(code=1011)
            return
        offered = websocket.headers.get("sec-websocket-protocol", "")
        subprotocol = "binary" if "binary" in offered else None
        await websocket.accept(subprotocol=subprotocol)
        url = endpoint.base_url.replace("http://", "ws://", 1) + "/view/websockify"
        try:
            upstream = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {endpoint.token}"},
                subprotocols=["binary"] if subprotocol else None,
                max_size=None,
            )
        except Exception:
            await websocket.close(code=1011)
            return

        async def to_browser() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await upstream.send(message.get("bytes") or (message.get("text") or "").encode())
                browsers.touch(handoff.user_id)

        async def from_browser() -> None:
            async for message in upstream:
                await websocket.send_bytes(
                    message if isinstance(message, bytes) else message.encode()
                )

        tasks = [asyncio.ensure_future(to_browser()), asyncio.ensure_future(from_browser())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    return router


def build_browser_api_router() -> APIRouter:
    """Apps' view of users' browsers: the sites they are signed in to."""
    router = APIRouter(prefix="/browser/users/{user_id}")

    def _browsers(request: Request):
        browsers = state_from_request(request).pool.get_browsers()
        if browsers is None:
            raise APIError(code="BROWSER_UNAVAILABLE", message="this host has no browsers", status_code=503)
        return browsers

    @router.get("/sites")
    async def list_sites(request: Request, user_id: str) -> dict[str, Any]:
        try:
            sites = await _browsers(request).sites(user_id)
        except _BROWSER_ERRORS as exc:
            raise APIError(code="BROWSER_UNAVAILABLE", message=str(exc), status_code=503) from exc
        return success({"user_id": user_id, "sites": sites})

    @router.delete("/sites/{domain}")
    async def clear_site(request: Request, user_id: str, domain: str) -> dict[str, Any]:
        try:
            cleared = await _browsers(request).clear_site(user_id, domain)
        except _BROWSER_ERRORS as exc:
            raise APIError(code="BROWSER_UNAVAILABLE", message=str(exc), status_code=503) from exc
        return success({"user_id": user_id, **cleared})

    @router.delete("")
    async def delete_profile(request: Request, user_id: str) -> dict[str, Any]:
        try:
            await _browsers(request).delete_profile(user_id)
        except _BROWSER_ERRORS as exc:
            raise APIError(code="BROWSER_UNAVAILABLE", message=str(exc), status_code=503) from exc
        return success({"user_id": user_id, "deleted": True})

    return router


def _view_error() -> Response:
    return Response(
        b"This sign-in link has expired or was already used.",
        status_code=404,
        media_type="text/plain",
    )


__all__ = [
    "build_browser_api_router",
    "build_browser_link_router",
    "build_browser_router",
    "view_url",
]

"""Starlette transport adapter for one Mash agent runtime."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .runtime import MashAgentRuntime


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


class MashAgentServer:
    """ASGI server surface exposing one runtime via H2A HTTP + SSE."""

    def __init__(self, runtime: MashAgentRuntime) -> None:
        self.runtime = runtime
        self.agent_id = runtime.app_id
        self._ready_event = asyncio.Event()
        self._startup_error: BaseException | None = None

        @asynccontextmanager
        async def _lifespan(app: Starlette):
            del app
            try:
                await self.runtime.open()
            except BaseException as exc:
                self._startup_error = exc
                self._ready_event.set()
                raise
            self._ready_event.set()
            try:
                yield
            finally:
                await self.runtime.shutdown()

        self.app = Starlette(
            debug=False,
            lifespan=_lifespan,
            routes=[
                Route("/health", self.health, methods=["GET"]),
                Route(
                    "/agent/{agent_id:str}/request",
                    self.submit_request,
                    methods=["POST"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}",
                    self.stream_request,
                    methods=["GET"],
                ),
            ],
        )

    @classmethod
    def from_spec(cls, definition) -> "MashAgentServer":
        return cls(MashAgentRuntime.from_spec(definition))

    async def wait_until_ready(self, *, timeout: float | None = None) -> None:
        try:
            if timeout is None:
                await self._ready_event.wait()
            else:
                await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"agent runtime '{self.agent_id}' did not become ready before timeout"
            ) from exc
        if self._startup_error is not None:
            raise RuntimeError(
                f"agent runtime '{self.agent_id}' failed during startup"
            ) from self._startup_error

    async def health(self, request: Request) -> Response:
        del request
        session_info = await self.runtime.get_session_info()
        return JSONResponse(
            {
                "status": "ok",
                "agent_id": self.agent_id,
                "app_id": self.runtime.app_id,
                "session": session_info,
            }
        )

    async def submit_request(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")

        if not isinstance(payload, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return _json_error(400, "INVALID_REQUEST", "message is required")

        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            return _json_error(400, "INVALID_REQUEST", "session_id must be a string")

        turn_metadata = payload.get("turn_metadata") or {}
        if not isinstance(turn_metadata, dict):
            return _json_error(
                400, "INVALID_REQUEST", "turn_metadata must be an object"
            )

        accepted = await self.runtime.submit_request(
            message=message,
            session_id=session_id,
            turn_metadata=turn_metadata,
        )
        return JSONResponse(accepted, status_code=202)

    async def stream_request(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        request_id = request.path_params.get("request_id", "").strip()
        if not request_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        if not self.runtime.has_request(request_id):
            return _json_error(404, "REQUEST_NOT_FOUND", "Request not found")

        async def _generate():
            cursor = 0
            while True:
                if await request.is_disconnected():
                    break
                events, cursor, done = await self.runtime.stream_request_events(
                    request_id,
                    cursor=cursor,
                    wait_timeout=15.0,
                )
                if events:
                    for event in events:
                        event_data_json = json.dumps(event["data"], ensure_ascii=True)
                        yield f"event: {event['event']}\n"
                        yield f"data: {event_data_json}\n\n"
                elif done:
                    break
                else:
                    yield ": keep-alive\n\n"

                if done:
                    break

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )

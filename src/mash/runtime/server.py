"""Starlette transport adapter for one agent runtime."""

from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .engine import InteractionClosedError
from .errors import RequestStaleError, RequestTerminalError
from .events import PostgresRuntimeStore
from ..otel.context import normalize_trace_context
from .requests import (
    RequestIdConflictError,
    parent_request_metadata,
    validate_mode,
    validate_request_id,
    validate_user_id,
)
from .service import AgentRuntime
from .structured_output import serialize_structured_output


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


class _BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every route but /health."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            response = _json_error(401, "UNAUTHORIZED", "Missing or invalid bearer token")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AgentServer:
    """ASGI server surface exposing one runtime via H2A HTTP + SSE.

    With ``token`` set, every route except ``/health`` requires
    ``Authorization: Bearer <token>``.
    """

    def __init__(self, runtime: AgentRuntime, *, token: str | None = None) -> None:
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
                await self.runtime.runtime_store.close()
                await self.runtime.store.close()

        self.app = Starlette(
            debug=False,
            lifespan=_lifespan,
            routes=[
                Route("/health", self.health, methods=["GET"]),
                Route(
                    "/agent/{agent_id:str}/describe",
                    self.describe,
                    methods=["GET"],
                ),
                Route(
                    "/agent/{agent_id:str}/session/{session_id:str}/compact",
                    self.compact_session,
                    methods=["POST"],
                ),
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
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/interaction",
                    self.post_interaction,
                    methods=["POST"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/status",
                    self.get_request_status,
                    methods=["GET"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/resume",
                    self.resume_request,
                    methods=["POST"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/cancel",
                    self.cancel_request,
                    methods=["POST"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/rerun",
                    self.rerun_request,
                    methods=["POST"],
                ),
                Route(
                    "/agent/{agent_id:str}/request/{request_id:str}/mode",
                    self.set_request_mode,
                    methods=["POST"],
                ),
            ],
        )
        if token:
            self.app.add_middleware(_BearerAuthMiddleware, token=token)

    @classmethod
    def from_spec(
        cls,
        definition,
        *,
        runtime_database_url: str | None = None,
        session_id: str,
        token: str | None = None,
    ) -> "AgentServer":
        from mash.core.database import resolve_database_url

        db_url = runtime_database_url or resolve_database_url()
        runtime_store = PostgresRuntimeStore(db_url)
        memory_store = definition.build_memory_store()
        return cls(
            AgentRuntime.from_spec(
                definition,
                runtime_database_url=runtime_database_url,
                session_id=session_id,
                runtime_store=runtime_store,
                memory_store=memory_store,
            ),
            token=token,
        )

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
        if not isinstance(session_id, str) or not session_id.strip():
            return _json_error(
                400,
                "INVALID_REQUEST",
                "session_id is required",
            )

        try:
            structured_output = serialize_structured_output(
                payload.get("structured_output")
            )
        except (TypeError, ValueError) as exc:
            return _json_error(400, "INVALID_STRUCTURED_OUTPUT", str(exc))

        metadata = payload.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return _json_error(400, "INVALID_REQUEST", "metadata must be an object")

        context = payload.get("context")
        if context is not None and not isinstance(context, str):
            return _json_error(400, "INVALID_REQUEST", "context must be a string")

        host = payload.get("host")
        if host is not None and not isinstance(host, dict):
            return _json_error(400, "INVALID_REQUEST", "host must be an object")

        parent = payload.get("parent")
        if parent is not None:
            try:
                parent_request_metadata(parent, agent_id=self.agent_id)
            except ValueError as exc:
                return _json_error(400, "INVALID_PARENT", str(exc))

        mode = payload.get("mode", "fg")
        try:
            validate_mode(mode)
        except ValueError as exc:
            return _json_error(400, "INVALID_REQUEST", str(exc))

        user_id = payload.get("user_id")
        if user_id is None:
            return _json_error(400, "INVALID_REQUEST", "user_id is required")
        try:
            validate_user_id(user_id)
        except ValueError as exc:
            return _json_error(400, "INVALID_REQUEST", str(exc))

        request_id = payload.get("request_id")
        if request_id is not None:
            try:
                validate_request_id(request_id)
            except ValueError as exc:
                return _json_error(400, "INVALID_REQUEST", str(exc))
            try:
                existing = await self.runtime.get_accepted_request(request_id)
            except RequestIdConflictError as exc:
                return _json_error(409, "REQUEST_ID_CONFLICT", str(exc))
            if existing is not None:
                return JSONResponse(existing, status_code=200)

        traceparent, tracestate = normalize_trace_context(
            payload.get("traceparent") if isinstance(payload.get("traceparent"), str) else None,
            payload.get("tracestate") if isinstance(payload.get("tracestate"), str) else None,
        )

        try:
            accepted = await self.runtime.submit_request(
                message=message,
                session_id=session_id.strip(),
                structured_output=structured_output,
                host_snapshot=host,
                context=context,
                metadata=metadata,
                request_id=request_id,
                parent=parent,
                user_id=user_id,
                mode=mode,
                traceparent=traceparent,
                tracestate=tracestate,
            )
        except RequestIdConflictError as exc:
            return _json_error(409, "REQUEST_ID_CONFLICT", str(exc))
        return JSONResponse(accepted, status_code=202)

    async def compact_session(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        session_id = request.path_params.get("session_id", "").strip()
        if not session_id:
            return _json_error(400, "INVALID_REQUEST", "session_id is required")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(payload, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")

        reason = payload.get("reason", "manual")
        if not isinstance(reason, str) or not reason.strip():
            return _json_error(400, "INVALID_REQUEST", "reason must be a non-empty string")
        tokens_reset = payload.get("session_total_tokens_reset", 0)
        if not isinstance(tokens_reset, int) or isinstance(tokens_reset, bool) or tokens_reset < 0:
            return _json_error(
                400,
                "INVALID_REQUEST",
                "session_total_tokens_reset must be a non-negative integer",
            )

        summary_text, trace_id = await self.runtime.compact_session(
            session_id=session_id,
            reason=reason.strip(),
            session_total_tokens_reset=tokens_reset,
        )
        return JSONResponse({"summary_text": summary_text, "trace_id": trace_id})

    async def describe(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        return JSONResponse(self.runtime.describe())

    async def post_interaction(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        request_id = request.path_params.get("request_id", "").strip()
        if not request_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        if not await self.runtime.runtime_store.has_request(request_id):
            return _json_error(404, "REQUEST_NOT_FOUND", "Request not found")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")

        if not isinstance(payload, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")

        interaction_id = payload.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            return _json_error(400, "INVALID_REQUEST", "interaction_id is required")

        try:
            await self.runtime.answer_interaction(
                request_id, interaction_id.strip(), payload.get("response")
            )
        except KeyError:
            return _json_error(404, "INTERACTION_NOT_FOUND", "Interaction not found")
        except InteractionClosedError as exc:
            return _json_error(409, "INTERACTION_CLOSED", str(exc))

        return JSONResponse({"ok": True, "interaction_id": interaction_id.strip()})

    async def get_request_status(self, request: Request) -> Response:
        return await self._control(request, self.runtime.get_request_status)

    async def resume_request(self, request: Request) -> Response:
        return await self._control(request, self.runtime.resume_request)

    async def cancel_request(self, request: Request) -> Response:
        return await self._control(request, self.runtime.cancel_request)

    async def rerun_request(self, request: Request) -> Response:
        return await self._control(request, self.runtime.rerun_request)

    async def set_request_mode(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        request_id = request.path_params.get("request_id", "").strip()
        if not request_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json_error(400, "INVALID_JSON", "Request body must be valid JSON")
        if not isinstance(payload, dict):
            return _json_error(400, "INVALID_REQUEST", "Request body must be an object")
        mode = payload.get("mode")
        try:
            validate_mode(mode)
        except ValueError as exc:
            return _json_error(400, "INVALID_REQUEST", str(exc))
        try:
            result = await self.runtime.set_request_mode(request_id, mode)
        except KeyError:
            return _json_error(404, "REQUEST_NOT_FOUND", "Request not found")
        except RequestTerminalError as exc:
            return _json_error(409, "REQUEST_TERMINAL", str(exc))
        return JSONResponse(result)

    async def _control(self, request: Request, operation) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        request_id = request.path_params.get("request_id", "").strip()
        if not request_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        try:
            result = await operation(request_id)
        except RequestStaleError as exc:
            return _json_error(409, "REQUEST_STALE", str(exc))
        except KeyError:
            return _json_error(404, "REQUEST_NOT_FOUND", "Request not found")
        return JSONResponse(result)

    async def stream_request(self, request: Request) -> Response:
        agent_id = request.path_params.get("agent_id", "").strip()
        if agent_id != self.agent_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")

        request_id = request.path_params.get("request_id", "").strip()
        if not request_id:
            return _json_error(404, "ROUTE_NOT_FOUND", "Route not found")
        if not await self.runtime.runtime_store.has_request(request_id):
            return _json_error(404, "REQUEST_NOT_FOUND", "Request not found")

        async def _generate():
            cursor = 0
            while True:
                if await request.is_disconnected():
                    break
                events, cursor, done = await self.runtime.stream_response_events(
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

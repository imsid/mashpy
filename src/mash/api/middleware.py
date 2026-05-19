"""ASGI middleware for backend API event logging."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .config import MashHostConfig
from .logging import (
    APIEvent,
    capture_body,
    query_params_from_scope,
    sanitize_headers,
)

logger = logging.getLogger(__name__)

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]


class APILoggingMiddleware:
    """Capture one API request/response event without changing API behavior."""

    def __init__(self, app: ASGIApp, *, config: MashHostConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._should_log(scope):
            await self.app(scope, receive, send)
            return

        started_at = time.time()
        method = str(scope.get("method") or "").upper()
        path = str(scope.get("path") or "")
        headers = list(scope.get("headers") or [])
        redacted_headers = {str(item).lower() for item in self.config.api_log_redacted_headers}
        request_headers = sanitize_headers(headers, redacted_headers)

        if _has_request_body(headers):
            request_body_bytes, replay_receive = await _capture_receive_body(receive)
        else:
            request_body_bytes = b""
            replay_receive = receive
        status_code = 500
        response_headers_raw: list[tuple[bytes, bytes]] = []
        response_body_chunks: list[bytes] = []
        response_content_type: str | None = None
        response_streaming = False

        async def logging_send(message: dict[str, Any]) -> None:
            nonlocal status_code, response_headers_raw, response_content_type, response_streaming
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 500)
                response_headers_raw = list(message.get("headers") or [])
                response_content_type = _first_header(response_headers_raw, b"content-type")
                message = {**message, "headers": response_headers_raw}
            elif message.get("type") == "http.response.body":
                body = message.get("body") or b""
                more_body = bool(message.get("more_body"))
                if more_body:
                    response_streaming = True
                if not response_streaming and _body_capture_allowed(response_content_type):
                    response_body_chunks.append(body)
            await send(message)

        app_error: BaseException | None = None
        try:
            await self.app(scope, replay_receive, logging_send)
        except BaseException as exc:
            app_error = exc
            raise
        finally:
            duration_ms = max(0, int((time.time() - started_at) * 1000))
            await self._append_event(
                scope=scope,
                method=method,
                path=path,
                request_headers=request_headers,
                response_headers=sanitize_headers(response_headers_raw, redacted_headers),
                request_body_bytes=request_body_bytes,
                response_body_bytes=b"".join(response_body_chunks),
                request_content_type=_first_header(headers, b"content-type"),
                response_content_type=response_content_type,
                response_streaming=response_streaming,
                status_code=status_code,
                duration_ms=duration_ms,
                error=app_error,
            )

    def _should_log(self, scope: dict[str, Any]) -> bool:
        if not self.config.api_logging_enabled:
            return False
        path = str(scope.get("path") or "")
        if self.config.api_log_api_only and not path.startswith(str(self.config.api_prefix)):
            return False
        return path not in {str(item) for item in self.config.api_log_excluded_paths}

    async def _append_event(
        self,
        *,
        scope: dict[str, Any],
        method: str,
        path: str,
        request_headers: dict[str, Any],
        response_headers: dict[str, Any],
        request_body_bytes: bytes,
        response_body_bytes: bytes,
        request_content_type: str | None,
        response_content_type: str | None,
        response_streaming: bool,
        status_code: int,
        duration_ms: int,
        error: BaseException | None,
    ) -> None:
        try:
            application = scope.get("app")
            state = getattr(application, "state", None)
            runtime_state = getattr(state, "runtime_state", None)
            store = getattr(runtime_state, "api_event_store", None)
            if store is None:
                return
            route = scope.get("route")
            request_body = capture_body(
                request_body_bytes,
                content_type=request_content_type,
                max_bytes=self.config.api_log_body_max_bytes,
                enabled=self.config.api_log_body_enabled,
            )
            if response_streaming:
                response_body = {
                    "content_type": response_content_type,
                    "bytes": len(response_body_bytes),
                    "truncated": False,
                    "capture_status": "streaming_skipped",
                }
            else:
                response_body = capture_body(
                    response_body_bytes,
                    content_type=response_content_type,
                    max_bytes=self.config.api_log_response_body_max_bytes,
                    enabled=self.config.api_log_body_enabled,
                )
            if error is not None:
                response_body = {
                    **response_body,
                    "error": type(error).__name__,
                    "capture_status": response_body.get("capture_status") or "error",
                }
            await store.append_event(
                APIEvent(
                    method=method,
                    path=path,
                    query_params={
                        "raw": (scope.get("query_string") or b"").decode("latin-1"),
                        "parsed": query_params_from_scope(scope),
                        "route": getattr(route, "path", None),
                        "path_params": dict(scope.get("path_params") or {}),
                    },
                    status_code=status_code,
                    duration_ms=duration_ms,
                    request_headers=request_headers,
                    response_headers=response_headers,
                    request_body=request_body,
                    response_body=response_body,
                    client_host=_client_host(scope),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            logger.warning("failed to append API event log: %s", exc)


async def _capture_receive_body(receive: Receive) -> tuple[bytes, Receive]:
    messages: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body", False):
            break

    index = 0

    async def replay_receive() -> dict[str, Any]:
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}

    return b"".join(chunks), replay_receive


def _first_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    lower_name = name.lower()
    for raw_name, raw_value in headers:
        if raw_name.lower() == lower_name:
            return raw_value.decode("latin-1", errors="replace")
    return None


def _has_request_body(headers: list[tuple[bytes, bytes]]) -> bool:
    content_length = _first_header(headers, b"content-length")
    if content_length is not None:
        try:
            return int(content_length) > 0
        except ValueError:
            return True
    transfer_encoding = _first_header(headers, b"transfer-encoding")
    return bool(transfer_encoding)


def _body_capture_allowed(content_type: str | None) -> bool:
    text = str(content_type or "").lower()
    return "text/event-stream" not in text


def _client_host(scope: dict[str, Any]) -> str | None:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return None

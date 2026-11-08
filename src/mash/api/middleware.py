"""ASGI middleware that caps the size of API request bodies.

FastAPI reads a route's body before it runs the route's dependencies, the API
key check among them, so without a cap anyone who can reach the host can make
it hold any amount of data. ``BodyLimitMiddleware`` refuses an ``/api/v1``
body over ``MAX_API_BODY_BYTES`` with 413 before the route sees it: at once
when ``Content-Length`` says so, otherwise after reading at most that much.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]

# Room for a message, its context and metadata, or a workflow's input.
MAX_API_BODY_BYTES = 4 * 1024 * 1024


class BodyLimitMiddleware:
    """Refuse a request body over ``max_bytes`` on paths under ``prefix``."""

    def __init__(
        self, app: ASGIApp, *, prefix: str, max_bytes: int = MAX_API_BODY_BYTES
    ) -> None:
        self.app = app
        self.prefix = prefix
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path") or "").startswith(
            self.prefix
        ):
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        content_length = _first_header(headers, b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = self.max_bytes + 1
            if declared > self.max_bytes:
                await _too_large(send, self.max_bytes)
                return
            # The server stops reading at Content-Length, so the body fits.
            await self.app(scope, receive, send)
            return
        if not _first_header(headers, b"transfer-encoding"):
            await self.app(scope, receive, send)
            return
        replay = await _read_body(receive, self.max_bytes)
        if replay is None:
            await _too_large(send, self.max_bytes)
            return
        await self.app(scope, replay, send)


async def _read_body(receive: Receive, max_bytes: int) -> Receive | None:
    """Read the body, or None as soon as it passes ``max_bytes``.

    Returns a ``receive`` that replays what was read.
    """
    messages: list[dict[str, Any]] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            break
        size += len(message.get("body") or b"")
        if size > max_bytes:
            return None
        if not message.get("more_body", False):
            break

    index = 0

    async def replay_receive() -> dict[str, Any]:
        # Replay the buffered body, then hand over to the server's own
        # receive, which waits for the next event (a disconnect). Answering
        # with empty bodies instead would never yield, and a streaming
        # response's disconnect listener would spin the event loop.
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return await receive()

    return replay_receive


async def _too_large(send: Send, max_bytes: int) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "BODY_TOO_LARGE",
                "message": f"request bodies are limited to {max_bytes} bytes",
                "details": {},
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _first_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    lower_name = name.lower()
    for raw_name, raw_value in headers:
        if raw_name.lower() == lower_name:
            return raw_value.decode("latin-1", errors="replace")
    return None

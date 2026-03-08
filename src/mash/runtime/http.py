"""HTTP transport for Mash agent server request/stream endpoints."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Protocol, cast
from urllib.parse import parse_qs, urlparse


class SupportsHTTPRuntime(Protocol):
    """Runtime contract used by the HTTP handler."""

    def submit_request(
        self,
        *,
        message: str,
        session_id: str | None = None,
        turn_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a request to the runtime."""
        raise NotImplementedError

    def has_request(self, request_id: str) -> bool:
        """Return whether the request exists."""
        raise NotImplementedError

    def stream_request_events(
        self,
        request_id: str,
        *,
        cursor: int = 0,
        wait_timeout: float = 15.0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Fetch streamed request events."""
        raise NotImplementedError

    def handle_control_request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle runtime control operations exposed via HTTP."""
        raise NotImplementedError


class MashAgentHTTPServer(ThreadingHTTPServer):
    """HTTP server wrapper for one MashAgentServer instance."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        runtime: SupportsHTTPRuntime,
        agent_id: str,
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.runtime = runtime
        self.agent_id = agent_id


class MashAgentHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler exposing POST+SSE agent endpoints."""

    server_version = "MashAgent/1.0"

    def _server(self) -> MashAgentHTTPServer:
        return cast(MashAgentHTTPServer, self.server)

    def do_POST(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        parsed = urlparse(self.path)
        server = self._server()
        if parsed.path == f"/agents/{server.agent_id}/control":
            body = self._read_json_body()
            if body is None:
                return
            action = body.get("action")
            if not isinstance(action, str) or not action.strip():
                self._json_error(400, "INVALID_REQUEST", "action is required")
                return
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                self._json_error(400, "INVALID_REQUEST", "payload must be an object")
                return
            try:
                response = server.runtime.handle_control_request(action, payload)
            except ValueError as exc:
                self._json_error(400, "INVALID_REQUEST", str(exc))
                return
            self._send_json(response, status=200)
            return

        if parsed.path != f"/agents/{server.agent_id}/requests":
            self._json_error(404, "ROUTE_NOT_FOUND", "Route not found")
            return

        body = self._read_json_body()
        if body is None:
            return

        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json_error(400, "INVALID_REQUEST", "message is required")
            return

        session_id = body.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            self._json_error(400, "INVALID_REQUEST", "session_id must be a string")
            return

        turn_metadata = body.get("turn_metadata") or {}
        if not isinstance(turn_metadata, dict):
            self._json_error(
                400,
                "INVALID_REQUEST",
                "turn_metadata must be an object",
            )
            return

        response = server.runtime.submit_request(
            message=message,
            session_id=session_id,
            turn_metadata=turn_metadata,
        )
        self._send_json(response, status=202)

    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        parsed = urlparse(self.path)
        server = self._server()
        control_path = f"/agents/{server.agent_id}/control"
        if parsed.path == control_path:
            params = parse_qs(parsed.query)
            action = params.get("action", [""])[0]
            if not action:
                self._json_error(400, "INVALID_REQUEST", "action is required")
                return
            payload: dict[str, Any] = {}
            for key, values in params.items():
                if key == "action":
                    continue
                if not values:
                    continue
                payload[key] = values[0]
            try:
                response = server.runtime.handle_control_request(action, payload)
            except ValueError as exc:
                self._json_error(400, "INVALID_REQUEST", str(exc))
                return
            self._send_json(response, status=200)
            return

        prefix = f"/agents/{server.agent_id}/requests/"
        if not parsed.path.startswith(prefix):
            self._json_error(404, "ROUTE_NOT_FOUND", "Route not found")
            return

        request_id = parsed.path[len(prefix) :].strip()
        if not request_id:
            self._json_error(404, "ROUTE_NOT_FOUND", "Route not found")
            return

        if not server.runtime.has_request(request_id):
            self._json_error(404, "REQUEST_NOT_FOUND", "Request not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        cursor = 0
        try:
            while True:
                events, cursor, done = server.runtime.stream_request_events(
                    request_id,
                    cursor=cursor,
                    wait_timeout=15.0,
                )
                if events:
                    for event in events:
                        event_data_json = json.dumps(event["data"], ensure_ascii=True)
                        self.wfile.write(f"event: {event['event']}\n".encode("utf-8"))
                        self.wfile.write(f"data: {event_data_json}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif done:
                    break
                else:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()

                if done:
                    break
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # pylint: disable=redefined-builtin
        del format, args
        return

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            self._json_error(400, "INVALID_REQUEST", "Content-Length is required")
            return None

        try:
            length = int(length_header)
        except ValueError:
            self._json_error(400, "INVALID_REQUEST", "Content-Length is invalid")
            return None

        raw = self.rfile.read(max(0, length))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_error(400, "INVALID_JSON", "Request body must be valid JSON")
            return None

        if not isinstance(payload, dict):
            self._json_error(400, "INVALID_REQUEST", "Request body must be an object")
            return None
        return payload

    def _json_error(self, status: int, code: str, message: str) -> None:
        self._send_json({"error": {"code": code, "message": message}}, status=status)

    def _send_json(self, payload: dict[str, Any], *, status: int) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


__all__ = ["MashAgentHTTPHandler", "MashAgentHTTPServer", "SupportsHTTPRuntime"]

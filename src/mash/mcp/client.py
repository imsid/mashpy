"""HTTP-based MCP client for interacting with MCP servers.

This module implements the core MCP handshake (initialize,
notifications/initialized, shutdown) using JSON-RPC over the official HTTP
transport. Mash supports server-initiated elicitation requests but does not
implement deprecated MCP features such as Sampling, Roots, or protocol-level
Logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_PROTOCOL_VERSION = "2025-03-26"
LOGGER = logging.getLogger("mash.mcp.client")
_HANDSHAKE_TIMEOUT_S = 30.0
_RPC_TIMEOUT_S = 60.0


class MCPClientError(RuntimeError):
    """Raised when the MCP HTTP client encounters a fatal error."""


@dataclass
class RPCResponse:
    """Container for a JSON-RPC result plus side-channel events."""

    result: Dict[str, Any]
    elicitation_requests: List[Dict[str, Any]]


class MCPHTTPClient:
    """Minimal async MCP HTTP client with elicitation hooks.

    Construction opens nothing; ``await connect()`` runs the handshake and
    starts the SSE listener as a task on the running loop.
    """

    def __init__(
        self,
        base_url: str,
        client_name: str,
        *,
        client_version: str = "0.1.0",
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        default_headers: Optional[Dict[str, str]] = None,
        elicitation_handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        self.base_url = self._normalize_url(base_url)
        self._http: Optional[httpx.AsyncClient] = None
        self.session_id: Optional[str] = None
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version = protocol_version
        self._elicitation_handler = elicitation_handler
        self._custom_headers: Dict[str, str] = {}
        if default_headers:
            for key, value in default_headers.items():
                if not isinstance(key, str):
                    continue
                if value is None:
                    continue
                self._custom_headers[key] = str(value)
        self._sse_task: Optional[asyncio.Task[None]] = None

    async def connect(self) -> None:
        """Run the MCP handshake and start listening for server messages."""
        self._http = httpx.AsyncClient()
        await self._initialize()
        self._start_sse_listener()

    # ------------------------------------------------------------------
    # Connection + lifecycle helpers
    # ------------------------------------------------------------------
    def _normalize_url(self, url: str) -> str:
        """Normalize MCP endpoint URL while preserving explicit paths/query params."""
        raw = (url or "").strip()
        parts = urlsplit(raw)

        path = parts.path or ""
        trimmed_path = path.rstrip("/")
        last_segment = trimmed_path.rsplit("/", 1)[-1]
        if not trimmed_path:
            normalized_path = "/mcp"
        elif last_segment.startswith("mcp"):
            # Path already names an MCP endpoint (e.g. /mcp, /mcp-oauth, /mcp-v2)
            normalized_path = trimmed_path
        elif "/mcp/" in trimmed_path:
            normalized_path = trimmed_path
        else:
            normalized_path = f"{trimmed_path}/mcp"

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                normalized_path,
                parts.query,
                parts.fragment,
            )
        )

    def _default_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["MCP-Session-ID"] = self.session_id
        if self._custom_headers:
            headers.update(self._custom_headers)
        return headers

    async def _post(
        self, payload: Dict[str, Any], *, timeout: float, label: str
    ) -> httpx.Response:
        if self._http is None:
            raise MCPClientError("Client is not connected")
        started = time.monotonic()
        try:
            response = await self._http.post(
                self.base_url,
                json=payload,
                headers=self._default_headers(),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise MCPClientError(f"{label} failed: {exc!r}") from exc
        LOGGER.debug(
            "MCP %s %s: HTTP %s in %d ms",
            self.client_name,
            label,
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    async def _initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        }
        response = await self._post(
            payload, timeout=_HANDSHAKE_TIMEOUT_S, label="initialize"
        )
        events = self._parse_events(response.text, response.headers.get("Content-Type"))
        self._extract_result_payload(payload["id"], events)
        self.session_id = response.headers.get("MCP-Session-ID")
        if not self.session_id:
            LOGGER.info(
                "Server did not return MCP-Session-ID header; continuing in stateless mode"
            )
        await self._send_notification("notifications/initialized")

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._post(payload, timeout=_HANDSHAKE_TIMEOUT_S, label=method)

    async def _send_rpc_response(self, request_id: Any, result: Dict[str, Any]) -> None:
        if request_id is None:
            return
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        await self._post(payload, timeout=_HANDSHAKE_TIMEOUT_S, label="response")

    async def aclose(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except BaseException:
                pass
            self._sse_task = None
        self.session_id = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Core RPC plumbing
    # ------------------------------------------------------------------
    async def _make_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> RPCResponse:
        request_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = await self._post(payload, timeout=_RPC_TIMEOUT_S, label=method)
        events = self._parse_events(response.text, response.headers.get("Content-Type"))
        rpc_result = self._extract_result_payload(request_id, events)
        elicitation = self._extract_interactions(events)
        return RPCResponse(
            result=rpc_result,
            elicitation_requests=elicitation,
        )

    @staticmethod
    def _parse_events(body: str, content_type: Optional[str]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        stripped = body.strip()
        if not stripped:
            return events

        ct_main = (content_type or "").split(";", 1)[0].strip().lower()
        if ct_main in ("", "application/json"):
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError as exc:  # pragma: no cover
                raise MCPClientError(f"Failed to decode JSON response: {exc}") from exc
            return events
        if ct_main == "text/event-stream":
            payload_lines: List[str] = []
            for raw_line in body.splitlines():
                if raw_line.startswith("data:"):
                    payload_lines.append(raw_line[5:].strip())
                elif not raw_line and payload_lines:
                    events.append(json.loads("".join(payload_lines)))
                    payload_lines = []
            if payload_lines:
                events.append(json.loads("".join(payload_lines)))
            return events

        raise MCPClientError(f"Unsupported Content-Type: {content_type or 'unknown'}")

    @staticmethod
    def _extract_result_payload(
        request_id: str, events: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        result: Optional[Dict[str, Any]] = None
        for event in events:
            if event.get("id") == request_id:
                if "error" in event:
                    raise MCPClientError(str(event["error"]))
                result = event.get("result") or {}
        if result is None:
            raise MCPClientError("Server response did not include a result")
        return result

    @staticmethod
    def _extract_interactions(
        events: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        elicitation: List[Dict[str, Any]] = []
        for event in events:
            method = event.get("method")
            if method == "sampling/createMessage":
                raise MCPClientError(
                    "Server requested deprecated MCP feature 'sampling/createMessage', "
                    "which Mash does not support."
                )
            if method in {
                "elicitation/createMessage",
                "elicitation/request",
            }:
                elicitation.append(event)
        return elicitation

    # ------------------------------------------------------------------
    # Public interface used by the manager
    # ------------------------------------------------------------------
    async def list_tools(self) -> List[Dict[str, Any]]:
        response = await self._make_request("tools/list")
        await self._handle_interactions(response)
        return response.result.get("tools", [])

    async def call_tool(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = await self._make_request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
        await self._handle_interactions(response)
        return response.result

    # ------------------------------------------------------------------
    # SSE listener for server-initiated messages
    # ------------------------------------------------------------------
    def _start_sse_listener(self) -> None:
        if self._sse_task or not self.session_id:
            return
        LOGGER.info("Starting SSE listener at %s", self.base_url)
        self._sse_task = asyncio.get_running_loop().create_task(
            self._run_sse_listener(), name=f"mcp-sse-{self.client_name}"
        )

    async def _run_sse_listener(self) -> None:
        if self._http is None:
            return
        headers = self._default_headers()
        headers["Accept"] = "text/event-stream"
        while True:
            try:
                async with self._http.stream(
                    "GET",
                    self.base_url,
                    headers=headers,
                    timeout=httpx.Timeout(None, connect=_HANDSHAKE_TIMEOUT_S),
                ) as response:
                    # A permanent failure (401, 403, 404, 405) stops the listener.
                    if response.status_code in (401, 403, 404, 405):
                        LOGGER.warning(
                            "SSE endpoint not available (HTTP %d). "
                            "Disabling SSE listener for this connection.",
                            response.status_code,
                        )
                        return
                    response.raise_for_status()
                    buffer: List[str] = []
                    LOGGER.info("SSE stream established")
                    async for raw_line in response.aiter_lines():
                        if not raw_line:
                            if buffer:
                                payload = "".join(buffer).strip()
                                buffer = []
                                if payload:
                                    await self._handle_sse_payload(payload)
                            continue
                        if raw_line.startswith("data:"):
                            buffer.append(raw_line[5:].strip())
            except httpx.HTTPError as exc:
                LOGGER.error("SSE connection error: %r", exc)
                await asyncio.sleep(1)

    async def _handle_sse_payload(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        method = message.get("method")
        LOGGER.debug("MCP %s SSE message: %s", self.client_name, method)
        if method == "sampling/createMessage":
            raise MCPClientError(
                "Server requested deprecated MCP feature 'sampling/createMessage', "
                "which Mash does not support."
            )
        if method in {"elicitation/createMessage", "elicitation/request"}:
            params = message.get("params", {})
            LOGGER.info("Received elicitation request via SSE")
            reply = self.handle_elicitation_request(params)
            await self._send_rpc_response(message.get("id"), reply)

    # ------------------------------------------------------------------
    # Elicitation hooks
    # ------------------------------------------------------------------
    async def _handle_interactions(self, response: RPCResponse) -> None:
        for elicitation_req in response.elicitation_requests:
            params = elicitation_req.get("params", {})
            elicitation_reply = self.handle_elicitation_request(params)
            await self._send_rpc_response(elicitation_req.get("id"), elicitation_reply)

    def handle_elicitation_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate elicitation requests to the configured handler."""

        return self._elicitation_handler(request)


__all__ = ["MCPHTTPClient", "MCPClientError"]

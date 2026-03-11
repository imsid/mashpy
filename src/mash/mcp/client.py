"""HTTP-based MCP client for interacting with MCP servers.

This module implements the core handshake described in the MCP client
concepts documentation (initialize, notifications/initialized, shutdown) using
JSON-RPC over the official HTTP transport. It also sketches how sampling and
elicitation hooks can work within the CLI workflow: servers can request that
clients obtain LLM completions (sampling) or ask the user for more input
(elicitation). For now those hooks surface helpful console guidance so we can
plug in a real model or UX later.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, TypeVar, Union
from urllib.parse import urlsplit, urlunsplit

import requests

DEFAULT_PROTOCOL_VERSION = "2025-03-26"
LOGGER = logging.getLogger("mash.mcp.client")


class MCPClientError(RuntimeError):
    """Raised when the MCP HTTP client encounters a fatal error."""


@dataclass
class RPCResponse:
    """Container for a JSON-RPC result plus side-channel events."""

    result: Dict[str, Any]
    sampling_requests: List[Dict[str, Any]]
    elicitation_requests: List[Dict[str, Any]]


SamplingHandler = Callable[
    [Dict[str, Any]], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]
]
T = TypeVar("T")


class MCPHTTPClient:
    """Minimal MCP HTTP client with sampling + elicitation hooks."""

    def __init__(
        self,
        base_url: str,
        client_name: str,
        *,
        client_version: str = "0.1.0",
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        default_headers: Optional[Dict[str, str]] = None,
        sampling_handler: SamplingHandler,
        elicitation_handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        self.base_url = self._normalize_url(base_url)
        self.session = requests.Session()
        self.session_id: Optional[str] = None
        self._initialized: bool = False
        self.server_info: Dict[str, Any] = {}
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version = protocol_version
        self._sampling_handler = sampling_handler
        self._elicitation_handler = elicitation_handler
        self._custom_headers: Dict[str, str] = {}
        if default_headers:
            for key, value in default_headers.items():
                if not isinstance(key, str):
                    continue
                if value is None:
                    continue
                self._custom_headers[key] = str(value)
        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()
        self._sse_url: Optional[str] = None
        self._initialize()
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
        if not trimmed_path:
            normalized_path = "/mcp"
        elif trimmed_path.endswith("/mcp"):
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

    def _initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {"sampling": {}},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        }
        response = self.session.post(
            self.base_url,
            json=payload,
            headers=self._default_headers(),
            timeout=30,
        )
        events = self._parse_events(response.text, response.headers.get("Content-Type"))
        self.server_info = self._extract_result_payload(payload["id"], events)
        self.session_id = response.headers.get(
            "MCP-Session-ID"
        ) or response.headers.get("mcp-session-id")
        if not self.session_id:
            LOGGER.info(
                "Server did not return MCP-Session-ID header; continuing in stateless mode"
            )
        self._initialized = True
        self._send_notification("notifications/initialized")

    def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        self.session.post(
            self.base_url,
            json=payload,
            headers=self._default_headers(),
            timeout=30,
        )

    def _send_rpc_response(self, request_id: Any, result: Dict[str, Any]) -> None:
        if request_id is None:
            return
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self.session.post(
            self.base_url,
            json=payload,
            headers=self._default_headers(),
            timeout=30,
        )

    def shutdown(self) -> None:
        if self._sse_thread:
            self._sse_stop.set()
            self._sse_thread.join(timeout=2)
            self._sse_thread = None
        self.session_id = None
        self._initialized = False
        self.session.close()

    def close(self) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Core RPC plumbing
    # ------------------------------------------------------------------
    def _make_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> RPCResponse:
        if not self._initialized and method != "initialize":
            raise MCPClientError("Client is not initialized")
        request_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        LOGGER.info("RPC request %s %s: %s", self.base_url, method, json.dumps(payload))
        response = self.session.post(
            self.base_url,
            json=payload,
            headers=self._default_headers(),
            timeout=60,
        )
        LOGGER.debug(
            "RPC response %s (status=%s): %s",
            method,
            response.status_code,
            response.text,
        )
        events = self._parse_events(response.text, response.headers.get("Content-Type"))
        rpc_result = self._extract_result_payload(request_id, events)
        sampling, elicitation = self._extract_interactions(events)
        return RPCResponse(
            result=rpc_result,
            sampling_requests=sampling,
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
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        sampling: List[Dict[str, Any]] = []
        elicitation: List[Dict[str, Any]] = []
        for event in events:
            if event.get("method") == "sampling/createMessage":
                sampling.append(event)
            elif event.get("method") in {
                "elicitation/createMessage",
                "elicitation/request",
            }:
                elicitation.append(event)
        return sampling, elicitation

    # ------------------------------------------------------------------
    # Public interface used by the host
    # ------------------------------------------------------------------
    def get_server_info(self) -> Dict[str, Any]:
        return self.server_info

    def _run_awaitable(self, awaitable: Coroutine[Any, Any, T]) -> T:
        """Run awaitable from sync call sites, even when an event loop is already running."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)

        result: dict[str, T] = {}
        error: dict[str, BaseException] = {}
        done = threading.Event()

        def _runner() -> None:
            try:
                result["value"] = asyncio.run(awaitable)
            except BaseException as exc:  # pragma: no cover - defensive
                error["value"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_runner, name="MCP-AwaitableRunner", daemon=True)
        try:
            thread.start()
        except Exception:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        done.wait()

        if "value" in error:
            raise error["value"]
        return result["value"]

    def list_tools(self) -> List[Dict[str, Any]]:
        response = self._make_request("tools/list")
        self._run_awaitable(self._handle_interactions(response))
        return response.result.get("tools", [])

    def list_resources(self) -> List[Dict[str, Any]]:
        response = self._make_request("resources/list")
        self._run_awaitable(self._handle_interactions(response))
        return response.result.get("resources", [])

    def list_resource_templates(self) -> List[Dict[str, Any]]:
        response = self._make_request("resources/templates/list")
        self._run_awaitable(self._handle_interactions(response))
        return response.result.get("resourceTemplates", [])

    def list_prompts(self) -> List[Dict[str, Any]]:
        response = self._make_request("prompts/list")
        self._run_awaitable(self._handle_interactions(response))
        return response.result.get("prompts", [])

    def read_resource(self, uri: str) -> Dict[str, Any]:
        response = self._make_request("resources/read", {"uri": uri})
        self._run_awaitable(self._handle_interactions(response))
        return response.result

    def get_prompt(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = self._make_request(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )
        self._run_awaitable(self._handle_interactions(response))
        return response.result

    def call_tool(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = self._make_request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
        self._run_awaitable(self._handle_interactions(response))
        return response.result

    # ------------------------------------------------------------------
    # SSE listener for server-initiated messages
    # ------------------------------------------------------------------
    def _start_sse_listener(self) -> None:
        if self._sse_thread or not self.session_id:
            return
        self._sse_url = self._resolve_sse_url()
        if not self._sse_url:
            LOGGER.info("Unable to determine SSE endpoint; skipping listener")
            return
        LOGGER.info("Starting SSE listener at %s", self._sse_url)
        self._sse_stop.clear()
        self._sse_thread = threading.Thread(
            target=self._run_sse_listener, name="MCP-SSE", daemon=True
        )
        self._sse_thread.start()

    def _run_sse_listener(self) -> None:
        if not self._sse_url:
            return
        headers = self._default_headers()
        headers["Accept"] = "text/event-stream"
        while not self._sse_stop.is_set():
            try:
                with self.session.get(
                    self._sse_url,
                    headers=headers,
                    stream=True,
                    timeout=None,
                ) as response:
                    response.raise_for_status()
                    buffer: List[str] = []
                    LOGGER.info("SSE stream established")
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if self._sse_stop.is_set():
                            break
                        if not raw_line:
                            if buffer:
                                payload = "".join(buffer).strip()
                                buffer = []
                                if payload:
                                    self._run_awaitable(self._handle_sse_payload(payload))
                            continue
                        if raw_line.startswith("data:"):
                            buffer.append(raw_line[5:].strip())
                if self._sse_stop.is_set():
                    break
            except requests.RequestException as exc:
                if self._sse_stop.is_set():
                    break

                # Check if this is a permanent failure (405, 404, 403, 401)
                if hasattr(exc, "response") and exc.response is not None:
                    status_code = exc.response.status_code
                    if status_code in (401, 403, 404, 405):
                        LOGGER.warning(
                            "SSE endpoint not available (HTTP %d). "
                            "Disabling SSE listener for this connection.",
                            status_code,
                        )
                        break  # Stop retrying for permanent failures

                LOGGER.error("SSE connection error: %s", exc)
                time.sleep(1)

    def _resolve_sse_url(self) -> Optional[str]:
        return self.base_url

    async def _handle_sse_payload(self, payload: str) -> None:
        try:
            message = json.loads(payload)
            LOGGER.info("SSE payload: %s", message)
        except json.JSONDecodeError:
            return
        method = message.get("method")
        if method == "sampling/createMessage":
            params = message.get("params", {})
            LOGGER.info("Received sampling request via SSE")
            reply = await self.handle_sampling_request(params)
            self._send_rpc_response(message.get("id"), reply)
        elif method in {"elicitation/createMessage", "elicitation/request"}:
            params = message.get("params", {})
            LOGGER.info("Received elicitation request via SSE")
            reply = self.handle_elicitation_request(params)
            self._send_rpc_response(message.get("id"), reply)

    # ------------------------------------------------------------------
    # Sampling + elicitation hooks
    # ------------------------------------------------------------------
    async def _handle_interactions(self, response: RPCResponse) -> None:
        for sampling_req in response.sampling_requests:
            params = sampling_req.get("params", {})
            sampling_reply = await self.handle_sampling_request(params)
            self._send_rpc_response(sampling_req.get("id"), sampling_reply)
        for elicitation_req in response.elicitation_requests:
            params = elicitation_req.get("params", {})
            elicitation_reply = self.handle_elicitation_request(params)
            self._send_rpc_response(elicitation_req.get("id"), elicitation_reply)

    async def handle_sampling_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate sampling requests to the configured handler."""
        result = self._sampling_handler(request)
        if inspect.isawaitable(result):
            return await result
        return result

    def handle_elicitation_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate elicitation requests to the configured handler."""

        return self._elicitation_handler(request)


__all__ = ["MCPHTTPClient", "MCPClientError"]

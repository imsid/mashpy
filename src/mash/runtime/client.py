"""Async H2A client for interacting with one agent runtime."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx


class AgentClientError(RuntimeError):
    """Raised when AgentClient operations fail."""


class AgentClient:
    """Dedicated client bound to exactly one agent runtime."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        *,
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.agent_id = agent_id.strip()
        if not self.agent_id:
            raise ValueError("agent_id is required")

        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if default_headers:
            for key, value in default_headers.items():
                if isinstance(key, str) and value is not None:
                    self._headers[key] = str(value)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parts = urlsplit((base_url or "").strip())
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def _request_url(self) -> str:
        return f"{self.base_url}/agent/{self.agent_id}/request"

    def _request_stream_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}"

    def _health_url(self) -> str:
        return f"{self.base_url}/health"

    @staticmethod
    def _extract_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        return response.text

    @staticmethod
    def _parse_raw_event(event_name: Optional[str], data_lines: list[str]) -> dict[str, Any] | None:
        if not event_name or not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return {"event": event_name, "data": payload}

    async def health(self, *, timeout: float = 5.0) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(headers=self._headers) as client:
                response = await client.get(
                    self._health_url(),
                    headers={"Accept": "application/json"},
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"health check failed: {exc}") from exc
        if response.status_code != 200:
            raise AgentClientError(
                f"health check failed (status={response.status_code}): {self._extract_message(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise AgentClientError("health response must be an object")
        return payload

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        timeout: float = 30.0,
    ) -> str:
        payload: Dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }

        try:
            async with httpx.AsyncClient(headers=self._headers) as client:
                response = await client.post(
                    self._request_url(),
                    json=payload,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"POST request failed: {exc}") from exc

        if response.status_code != 202:
            raise AgentClientError(
                f"POST request failed (status={response.status_code}): {self._extract_message(response)}"
            )

        data = response.json()
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            raise AgentClientError("Agent POST response missing request_id")
        return request_id

    async def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        headers = dict(self._headers)
        headers["Accept"] = "text/event-stream"
        timeout_config = None if timeout is None else httpx.Timeout(timeout)

        try:
            async with httpx.AsyncClient(headers=self._headers) as client:
                async with client.stream(
                    "GET",
                    self._request_stream_url(request_id),
                    headers=headers,
                    timeout=timeout_config,
                ) as response:
                    if response.status_code != 200:
                        raise AgentClientError(
                            f"GET stream failed (status={response.status_code}): {self._extract_message(response)}"
                        )

                    event_name: Optional[str] = None
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line is None:
                            continue
                        stripped = line.strip()
                        if not stripped:
                            parsed = self._parse_raw_event(event_name, data_lines)
                            if parsed is not None:
                                yield parsed
                            event_name = None
                            data_lines = []
                            continue
                        if stripped.startswith(":"):
                            continue
                        if stripped.startswith("event:"):
                            event_name = stripped[6:].strip()
                            continue
                        if stripped.startswith("data:"):
                            data_lines.append(stripped[5:].strip())
        except httpx.HTTPError as exc:
            raise AgentClientError(f"GET stream failed: {exc}") from exc

    async def invoke(
        self,
        message: str,
        *,
        session_id: str,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        request_id = await self.post_request(
            message,
            session_id=session_id,
        )

        started = time.time()
        timeout_seconds = None if timeout_ms is None else max(1, int(timeout_ms)) / 1000.0

        async for event in self.stream_response(request_id, timeout=timeout_seconds):
            event_name = str(event.get("event") or "")
            data = event.get("data")
            if event_name == "request.completed":
                if not isinstance(data, dict):
                    raise AgentClientError("request.completed payload is invalid")
                return data
            if event_name == "request.error":
                error_message = "request failed"
                if isinstance(data, dict) and isinstance(data.get("error"), str):
                    error_message = data["error"]
                raise AgentClientError(error_message)
            if timeout_seconds is not None and time.time() - started > timeout_seconds:
                raise TimeoutError("agent invoke timed out")

        raise AgentClientError("SSE stream ended without a terminal event")

    async def close(self) -> None:
        return None


class InProcessAgentClient(AgentClient):
    """Client adapter for talking to an in-process runtime without HTTP."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.agent_id = runtime.app_id
        self.base_url = f"inproc://{self.agent_id}"
        self._headers: Dict[str, str] = {}

    async def health(self, *, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        session_info = await self.runtime.get_session_info()
        return {
            "status": "ok",
            "agent_id": self.agent_id,
            "app_id": self.runtime.app_id,
            "session": session_info,
        }

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        timeout: float = 30.0,
    ) -> str:
        del timeout
        accepted = await self.runtime.submit_request(
            message=message,
            session_id=session_id,
        )
        request_id = str(accepted.get("request_id") or "").strip()
        if not request_id:
            raise AgentClientError("Agent POST response missing request_id")
        return request_id

    async def post_subagent_request(
        self,
        message: str,
        *,
        session_id: str,
        primary_session_id: str,
        primary_app_id: str,
        subagent_id: str,
        subagent_invoke_opts: Dict[str, Any],
        timeout: float = 30.0,
    ) -> str:
        del timeout
        accepted = await self.runtime.submit_subagent_request(
            message=message,
            session_id=session_id,
            primary_session_id=primary_session_id,
            primary_app_id=primary_app_id,
            subagent_id=subagent_id,
            subagent_invoke_opts=subagent_invoke_opts,
        )
        request_id = str(accepted.get("request_id") or "").strip()
        if not request_id:
            raise AgentClientError("Agent POST response missing request_id")
        return request_id

    async def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        cursor = 0
        started_at = time.time()
        poll_timeout = 0.25
        while True:
            elapsed = time.time() - started_at
            if timeout is not None and elapsed > timeout:
                raise TimeoutError("agent stream timed out")

            wait_timeout = poll_timeout
            if timeout is not None:
                wait_timeout = max(0.0, min(poll_timeout, timeout - elapsed))
            events, cursor, done = await self.runtime.stream_response_events(
                request_id,
                cursor=cursor,
                wait_timeout=wait_timeout,
            )
            for event in events:
                yield event
            if done:
                return


__all__ = ["AgentClient", "AgentClientError", "InProcessAgentClient"]

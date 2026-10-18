"""Async H2A HTTP client for one agent server."""

from __future__ import annotations

import contextlib
import json
from typing import Any, AsyncIterator, Dict, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .structured_output import serialize_structured_output


class AgentClientError(RuntimeError):
    """Raised when AgentClient operations fail."""


class AgentUnavailableError(AgentClientError):
    """The agent's server could not be reached (for example, its computer is down)."""


class AgentClientLike(Protocol):
    """Shared transport contract for one addressable agent runtime."""

    agent_id: str

    async def health(self, *, timeout: float = 5.0) -> dict[str, Any]: ...

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        structured_output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        context: Optional[str] = None,
        host: Optional[Dict[str, Any]] = None,
        parent: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> str: ...

    async def describe(self, *, timeout: float = 10.0) -> dict[str, Any]: ...

    async def compact_session(
        self,
        session_id: str,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
        timeout: float = 120.0,
    ) -> dict[str, Any]: ...

    async def post_interaction(
        self,
        request_id: str,
        *,
        interaction_id: str,
        response: Any,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

    def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...

    async def get_request_status(
        self,
        request_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]: ...

    async def resume_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

    async def cancel_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

    async def rerun_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class AgentClient:
    """Dedicated HTTP client bound to exactly one agent runtime."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        *,
        token: Optional[str] = None,
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
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._client: httpx.AsyncClient | None = None

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        # One httpx client for the AgentClient's lifetime: building one per
        # call costs ~15ms (a fresh TLS context) on every request and stream.
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self._headers)
        yield self._client

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parts = urlsplit((base_url or "").strip())
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def _request_url(self) -> str:
        return f"{self.base_url}/agent/{self.agent_id}/request"

    def _request_stream_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}"

    def _interaction_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/interaction"

    def _request_status_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/status"

    def _request_resume_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/resume"

    def _request_cancel_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/cancel"

    def _request_rerun_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/rerun"

    def _health_url(self) -> str:
        return f"{self.base_url}/health"

    def _describe_url(self) -> str:
        return f"{self.base_url}/agent/{self.agent_id}/describe"

    def _compact_url(self, session_id: str) -> str:
        return f"{self.base_url}/agent/{self.agent_id}/session/{session_id}/compact"

    def _http_error(self, operation: str, exc: httpx.HTTPError) -> AgentClientError:
        if isinstance(exc, httpx.TransportError):
            return AgentUnavailableError(
                f"agent '{self.agent_id}' is unavailable ({operation}): {exc}"
            )
        return AgentClientError(f"{operation}: {exc}")

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
    def _parse_raw_event(
        event_name: Optional[str], data_lines: list[str]
    ) -> dict[str, Any] | None:
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
            async with self._session() as client:
                response = await client.get(
                    self._health_url(),
                    headers={"Accept": "application/json"},
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("health check failed", exc) from exc
        if response.status_code != 200:
            raise AgentClientError(
                f"health check failed (status={response.status_code}): {self._extract_message(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise AgentClientError("health response must be an object")
        return payload

    async def describe(self, *, timeout: float = 10.0) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.get(
                    self._describe_url(),
                    headers={"Accept": "application/json"},
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("describe failed", exc) from exc
        if response.status_code != 200:
            raise AgentClientError(
                f"describe failed (status={response.status_code}): {self._extract_message(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise AgentClientError("describe response must be an object")
        return payload

    async def compact_session(
        self,
        session_id: str,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.post(
                    self._compact_url(session_id),
                    json={
                        "reason": reason,
                        "session_total_tokens_reset": session_total_tokens_reset,
                    },
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("compact failed", exc) from exc
        if response.status_code != 200:
            raise AgentClientError(
                f"compact failed (status={response.status_code}): {self._extract_message(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise AgentClientError("compact response must be an object")
        return payload

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        structured_output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        context: Optional[str] = None,
        host: Optional[Dict[str, Any]] = None,
        parent: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> str:
        """Submit a request. Resubmitting a known ``request_id`` returns it again."""
        payload: Dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }
        if structured_output is not None:
            payload["structured_output"] = serialize_structured_output(
                structured_output
            )
        if metadata:
            payload["metadata"] = dict(metadata)
        if request_id is not None:
            payload["request_id"] = request_id
        if context is not None:
            payload["context"] = context
        if host is not None:
            payload["host"] = dict(host)
        if parent is not None:
            payload["parent"] = dict(parent)

        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_url(),
                    json=payload,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST request failed", exc) from exc

        if response.status_code not in (200, 202):
            raise AgentClientError(
                f"POST request failed (status={response.status_code}): {self._extract_message(response)}"
            )

        data = response.json()
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            raise AgentClientError("Agent POST response missing request_id")
        return request_id

    async def post_interaction(
        self,
        request_id: str,
        *,
        interaction_id: str,
        response: Any,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        payload = {
            "interaction_id": interaction_id,
            "response": response,
        }
        try:
            async with self._session() as client:
                resp = await client.post(
                    self._interaction_url(request_id),
                    json=payload,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST interaction failed", exc) from exc

        if resp.status_code == 404:
            raise AgentClientError(
                f"POST interaction failed (status=404): {self._extract_message(resp)}"
            )
        if resp.status_code == 409:
            raise AgentClientError(
                "POST interaction failed (status=409): interaction already responded"
            )
        if resp.status_code == 410:
            raise AgentClientError(
                "POST interaction failed (status=410): interaction timed out"
            )
        if resp.status_code != 200:
            raise AgentClientError(
                f"POST interaction failed (status={resp.status_code}): {self._extract_message(resp)}"
            )
        return resp.json()

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
            async with self._session() as client:
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
            raise self._http_error("GET stream failed", exc) from exc

    async def get_request_status(
        self,
        request_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.get(
                    self._request_status_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("GET request status failed", exc) from exc
        if response.status_code == 404:
            raise AgentClientError(
                f"request '{request_id}' not found"
            )
        if response.status_code != 200:
            raise AgentClientError(
                f"GET request status failed (status={response.status_code}): "
                f"{self._extract_message(response)}"
            )
        return response.json()

    async def resume_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_resume_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST resume request failed", exc) from exc
        if response.status_code == 404:
            raise AgentClientError(
                f"request '{request_id}' not found"
            )
        if response.status_code != 200:
            raise AgentClientError(
                f"POST resume request failed (status={response.status_code}): "
                f"{self._extract_message(response)}"
            )
        return response.json()

    async def cancel_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_cancel_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST cancel request failed", exc) from exc
        if response.status_code == 404:
            raise AgentClientError(f"request '{request_id}' not found")
        if response.status_code != 200:
            raise AgentClientError(
                f"POST cancel request failed (status={response.status_code}): "
                f"{self._extract_message(response)}"
            )
        return response.json()

    async def rerun_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_rerun_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST rerun request failed", exc) from exc
        if response.status_code == 404:
            raise AgentClientError(f"request '{request_id}' not found")
        if response.status_code != 200:
            raise AgentClientError(
                f"POST rerun request failed (status={response.status_code}): "
                f"{self._extract_message(response)}"
            )
        return response.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "AgentClient",
    "AgentClientError",
    "AgentClientLike",
    "AgentUnavailableError",
]

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
    """The agent's server could not be reached (for example, its process is restarting)."""


class RequestModeConflictError(AgentClientError):
    """The request has finished, so its mode cannot change (HTTP 409)."""


class SessionUserConflictError(AgentClientError):
    """The session belongs to another user, so the request was refused (HTTP 409)."""


class InteractionClosedError(AgentClientError):
    """The interaction already has an answer, a timeout or a cancel (HTTP 409)."""


class InvalidInteractionResponseError(AgentClientError):
    """The answer is not one the interaction's schema allows (HTTP 400)."""


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
        user_id: Optional[str] = None,
        mode: Optional[str] = None,
        traceparent: Optional[str] = None,
        tracestate: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str: ...

    async def set_request_mode(
        self,
        request_id: str,
        mode: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]: ...

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


# Calls that answer at once: submit, status, cancel, resume, answers, health.
CONTROL_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)
# Event streams: one open connection per watcher, no cap.
STREAM_LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=20)
STREAM_CONNECT_TIMEOUT = 5.0


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
        self._stream_client: httpx.AsyncClient | None = None

    # Two httpx clients for the AgentClient's lifetime: building one per call
    # costs ~15ms (a fresh TLS context). Event streams stay open for as long
    # as a request runs, hours when it waits on the user, so they get a client
    # of their own: however many streams are open, submits, cancels and
    # answers still find a connection.

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self._headers, limits=CONTROL_LIMITS
            )
        yield self._client

    @contextlib.asynccontextmanager
    async def _stream_session(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._stream_client is None:
            self._stream_client = httpx.AsyncClient(
                headers=self._headers, limits=STREAM_LIMITS
            )
        yield self._stream_client

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

    def _request_mode_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/mode"

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
    def _error_code(response: httpx.Response) -> str | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) else None

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
        user_id: Optional[str] = None,
        mode: Optional[str] = None,
        traceparent: Optional[str] = None,
        tracestate: Optional[str] = None,
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
        if user_id is not None:
            payload["user_id"] = user_id
        if mode is not None:
            payload["mode"] = mode
        if traceparent is not None:
            payload["traceparent"] = traceparent
            if tracestate is not None:
                payload["tracestate"] = tracestate

        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_url(),
                    json=payload,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST request failed", exc) from exc

        if response.status_code == 409 and self._error_code(response) == "SESSION_USER_MISMATCH":
            raise SessionUserConflictError(self._extract_message(response))
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
        if resp.status_code == 400:
            raise InvalidInteractionResponseError(self._extract_message(resp))
        if resp.status_code == 409:
            raise InteractionClosedError(self._extract_message(resp))
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
        # No read timeout by default: a stream is quiet while the request
        # waits on the user. Connecting and getting a connection still fail
        # fast.
        timeout_config = httpx.Timeout(
            connect=STREAM_CONNECT_TIMEOUT,
            read=timeout,
            write=STREAM_CONNECT_TIMEOUT,
            pool=STREAM_CONNECT_TIMEOUT,
        )

        try:
            async with self._stream_session() as client:
                async with client.stream(
                    "GET",
                    self._request_stream_url(request_id),
                    headers=headers,
                    timeout=timeout_config,
                ) as response:
                    if response.status_code != 200:
                        # A streamed body is not loaded until read; the error
                        # message comes from it.
                        await response.aread()
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

    async def set_request_mode(
        self,
        request_id: str,
        mode: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Switch a running request to ``fg`` or ``bg``."""
        try:
            async with self._session() as client:
                response = await client.post(
                    self._request_mode_url(request_id),
                    json={"mode": mode},
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise self._http_error("POST request mode failed", exc) from exc
        if response.status_code == 404:
            raise AgentClientError(f"request '{request_id}' not found")
        if response.status_code == 409:
            raise RequestModeConflictError(self._extract_message(response))
        if response.status_code != 200:
            raise AgentClientError(
                f"POST request mode failed (status={response.status_code}): "
                f"{self._extract_message(response)}"
            )
        return response.json()

    async def close(self) -> None:
        for client in (self._client, self._stream_client):
            if client is not None:
                await client.aclose()
        self._client = None
        self._stream_client = None


class WorkflowInputError(AgentClientError):
    """The workflow's input model refused the input."""

    def __init__(self, errors: list[Any]) -> None:
        self.errors = list(errors)
        super().__init__("workflow input validation failed")


class WorkflowRelayClient:
    """An agent process's calls to the host relay's workflow routes."""

    def __init__(self, base_url: str, *, token: Optional[str] = None) -> None:
        self.base_url = AgentClient._normalize_base_url(base_url)
        self._headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._client: httpx.AsyncClient | None = None

    def _session(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self._headers, limits=CONTROL_LIMITS)
        return self._client

    @staticmethod
    def _error(operation: str, response: httpx.Response) -> AgentClientError:
        return AgentClientError(
            f"{operation} failed (status={response.status_code}): "
            f"{AgentClient._extract_message(response)}"
        )

    async def post_workflow_run(
        self,
        workflow_id: str,
        *,
        parent_request_id: str,
        workflow_input: Dict[str, Any],
        dedup_key: Optional[str] = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Start a run for the parent request's user; a known ``dedup_key`` returns its run."""
        payload: Dict[str, Any] = {
            "parent": {"request_id": parent_request_id},
            "input": dict(workflow_input),
        }
        if dedup_key is not None:
            payload["dedup_key"] = dedup_key
        try:
            response = await self._session().post(
                f"{self.base_url}/workflow/{workflow_id}/run", json=payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(f"POST workflow run failed: {exc}") from exc
        if response.status_code == 422:
            try:
                error = response.json().get("error") or {}
            except ValueError:
                error = {}
            raise WorkflowInputError(list(error.get("details") or []))
        if response.status_code not in (200, 202):
            raise self._error("POST workflow run", response)
        return response.json()

    async def get_workflow_run(
        self,
        workflow_id: str,
        run_id: str,
        *,
        parent_request_id: str,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """The run's ``status``, ``result`` and ``error``."""
        try:
            response = await self._session().get(
                f"{self.base_url}/workflow/{workflow_id}/runs/{run_id}",
                params={"parent_request_id": parent_request_id},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(f"GET workflow run failed: {exc}") from exc
        if response.status_code != 200:
            raise self._error("GET workflow run", response)
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
    "RequestModeConflictError",
    "SessionUserConflictError",
    "WorkflowInputError",
    "WorkflowRelayClient",
]

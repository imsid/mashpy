"""Async H2A clients for interacting with one agent runtime."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .structured_output import serialize_structured_output


class AgentClientError(RuntimeError):
    """Raised when AgentClient operations fail."""


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
        timeout: float = 30.0,
    ) -> str: ...

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

    async def close(self) -> None: ...


class AgentClient:
    """Dedicated HTTP client bound to exactly one agent runtime."""

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

    def _interaction_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/interaction"

    def _request_status_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/status"

    def _request_resume_url(self, request_id: str) -> str:
        return f"{self._request_url()}/{request_id}/resume"

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
        structured_output: Any = None,
        timeout: float = 30.0,
    ) -> str:
        payload: Dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }
        if structured_output is not None:
            payload["structured_output"] = serialize_structured_output(
                structured_output
            )

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
            async with httpx.AsyncClient(headers=self._headers) as client:
                resp = await client.post(
                    self._interaction_url(request_id),
                    json=payload,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"POST interaction failed: {exc}") from exc

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

    async def get_request_status(
        self,
        request_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(headers=self._headers) as client:
                response = await client.get(
                    self._request_status_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"GET request status failed: {exc}") from exc
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
            async with httpx.AsyncClient(headers=self._headers) as client:
                response = await client.post(
                    self._request_resume_url(request_id),
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise AgentClientError(f"POST resume request failed: {exc}") from exc
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

    async def close(self) -> None:
        return None


class InProcessAgentClient:
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
        structured_output: Any = None,
        timeout: float = 30.0,
    ) -> str:
        del timeout
        accepted = await self.runtime.submit_request(
            message=message,
            session_id=session_id,
            structured_output=structured_output,
        )
        request_id = str(accepted.get("request_id") or "").strip()
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
        del timeout
        from dbos import DBOS as _DBOS

        from .engine.workflow import workflow_id_for

        wf_id = workflow_id_for(self.agent_id, request_id)
        await _DBOS.send_async(wf_id, response, topic=interaction_id)
        return {"ok": True, "interaction_id": interaction_id}

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

    async def get_request_status(
        self,
        request_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        del timeout
        return await self.runtime.get_request_status(request_id)

    async def resume_request(
        self,
        request_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        del timeout
        return await self.runtime.resume_request(request_id)

    async def close(self) -> None:
        return None


__all__ = ["AgentClient", "AgentClientError", "AgentClientLike", "InProcessAgentClient"]

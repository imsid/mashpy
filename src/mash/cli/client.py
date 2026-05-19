"""Remote HTTP client for Mash host deployments."""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests

DEFAULT_REQUEST_TIMEOUT = (10, 30)
DEFAULT_STREAM_TIMEOUT = (10, None)


class MashHostClientError(RuntimeError):
    """Raised when Mash host operations fail."""


class MashHostClient:
    """Thin client for interacting with a Mash host deployment."""

    def __init__(self, base_url: str, *, api_key: str | None = None) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self._session = requests.Session()
        self._headers = {"Accept": "application/json, text/event-stream"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parts = urlsplit((base_url or "").strip())
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def _url(self, path: str, *, query: dict[str, Any] | None = None) -> str:
        if query:
            return f"{self.base_url}{path}?{urlencode(query)}"
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        stream: bool = False,
        timeout: tuple[float, Optional[float]] | float | None = None,
    ) -> requests.Response:
        headers = dict(self._headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = self._session.request(
            method=method,
            url=self._url(path, query=query),
            headers=headers,
            json=json_body,
            stream=stream,
            timeout=(
                DEFAULT_STREAM_TIMEOUT
                if stream and timeout is None
                else (timeout or DEFAULT_REQUEST_TIMEOUT)
            ),
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = (
                payload.get("error", {}).get("message")
                if isinstance(payload, dict)
                else None
            ) or response.text
            raise MashHostClientError(
                f"{method} {path} failed ({response.status_code}): {message}"
            )
        return response

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/api/v1/health")
        return response.json()["data"]

    def list_agents(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/agent")
        payload = response.json()["data"]
        agents = payload.get("agents")
        if not isinstance(agents, list):
            return []
        return [agent for agent in agents if isinstance(agent, dict)]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/api/v1/agent/{quote(agent_id, safe='')}")
        return response.json()["data"]

    def submit_request(
        self,
        agent_id: str,
        *,
        message: str,
        session_id: str,
    ) -> str:
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request",
            json_body={
                "message": message,
                "session_id": session_id,
            },
        )
        return str(response.json()["data"]["request_id"])

    def stream_request(
        self, agent_id: str, request_id: str
    ) -> Iterator[dict[str, Any]]:
        with self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/events",
            stream=True,
        ) as response:
            event_name: Optional[str] = None
            data_lines: list[str] = []
            for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                if line is None:
                    continue
                stripped = line.strip()
                if not stripped:
                    if event_name and data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            payload = {"raw": raw}
                        yield {"event": event_name, "data": payload}
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

    def list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET", f"/api/v1/agent/{quote(agent_id, safe='')}/sessions"
        )
        sessions = response.json()["data"].get("sessions")
        if not isinstance(sessions, list):
            return []
        return [session for session in sessions if isinstance(session, dict)]

    def get_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}",
        )
        return response.json()["data"]

    def get_history(
        self, agent_id: str, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = {"limit": limit} if limit is not None else None
        response = self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}/history",
            query=query,
        )
        turns = response.json()["data"].get("turns")
        if not isinstance(turns, list):
            return []
        return [turn for turn in turns if isinstance(turn, dict)]

    def get_reasoning_trace(
        self,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/telemetry/reasoning-trace",
            query={
                "agent_id": agent_id,
                "session_id": session_id,
                "trace_id": trace_id,
            },
        )
        return response.json()["data"]

    def list_workflows(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/workflows")
        workflows = response.json()["data"].get("workflows")
        if not isinstance(workflows, list):
            return []
        return [workflow for workflow in workflows if isinstance(workflow, dict)]

    def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if dedup_key is not None:
            body["dedup_key"] = dedup_key
        if workflow_input is not None:
            body["input"] = workflow_input
        response = self._request(
            "POST",
            f"/api/v1/workflows/{quote(workflow_id, safe='')}/run",
            json_body=body,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def get_workflow_run(self, workflow_id: str, run_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/workflows/{quote(workflow_id, safe='')}/runs/{quote(run_id, safe='')}",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        self._session.close()

"""Remote HTTP client for Mash host deployments."""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests

from mash.runtime.structured_output import serialize_structured_output

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

    @staticmethod
    def _iter_sse_events(response: requests.Response) -> Iterator[dict[str, Any]]:
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

    def list_hosts(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/hosts")
        hosts = response.json()["data"].get("hosts")
        if not isinstance(hosts, list):
            return []
        return [host for host in hosts if isinstance(host, dict)]

    def get_host(self, host_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/api/v1/hosts/{quote(host_id, safe='')}")
        return response.json()["data"]

    def define_host(
        self,
        host_id: str,
        *,
        primary: str,
        subagents: list[str] | None = None,
        workflows: list[str] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/api/v1/hosts/{quote(host_id, safe='')}",
            json_body={
                "primary": primary,
                "subagents": list(subagents or []),
                "workflows": list(workflows or []),
            },
        )
        return response.json()["data"]

    def submit_host_request(
        self,
        host_id: str,
        *,
        message: str,
        session_id: str,
        structured_output: Any = None,
    ) -> dict[str, Any]:
        json_body: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }
        if structured_output is not None:
            json_body["structured_output"] = serialize_structured_output(structured_output)
        response = self._request(
            "POST",
            f"/api/v1/hosts/{quote(host_id, safe='')}/request",
            json_body=json_body,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def register_agent_skill(
        self,
        agent_id: str,
        skill_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/skill",
            json_body=dict(skill_payload),
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def register_agent_workflow(
        self,
        agent_id: str,
        workflow_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/workflow",
            json_body=dict(workflow_payload),
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def submit_request(
        self,
        agent_id: str,
        *,
        message: str,
        session_id: str,
        structured_output: Any = None,
    ) -> str:
        json_body: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
        }
        if structured_output is not None:
            json_body["structured_output"] = serialize_structured_output(structured_output)
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request",
            json_body=json_body,
        )
        return str(response.json()["data"]["request_id"])

    def post_interaction(
        self,
        agent_id: str,
        request_id: str,
        *,
        interaction_id: str,
        response: Any,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/interaction",
            json_body={"interaction_id": interaction_id, "response": response},
        )
        return resp.json().get("data", {})

    def stream_request(
        self, agent_id: str, request_id: str
    ) -> Iterator[dict[str, Any]]:
        with self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/events",
            stream=True,
        ) as response:
            yield from self._iter_sse_events(response)

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
            (
                f"/api/v1/agent/{quote(agent_id, safe='')}"
                f"/session/{quote(session_id, safe='')}"
                f"/trace/{quote(trace_id, safe='')}/reasoning"
            ),
        )
        return response.json()["data"]

    def list_traces(
        self,
        agent_id: str,
        session_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/api/v1/telemetry/traces",
            query={"agent_id": agent_id, "session_id": session_id, "limit": limit},
        )
        traces = response.json()["data"].get("traces")
        if not isinstance(traces, list):
            return []
        return traces

    def get_trace_analysis(
        self,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/telemetry/trace/analysis",
            query={"agent_id": agent_id, "session_id": session_id, "trace_id": trace_id, "stitch": "true"},
        )
        return response.json()["data"]

    def submit_feedback(
        self,
        agent_id: str,
        *,
        message: str,
        feedback_type: str = "text",
        host_id: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "message": message,
            "feedback_type": feedback_type,
        }
        if host_id is not None:
            body["host_id"] = host_id
        if session_id is not None:
            body["session_id"] = session_id
        if request_id is not None:
            body["request_id"] = request_id
        if trace_id is not None:
            body["trace_id"] = trace_id
        if context is not None:
            body["context"] = context
        response = self._request("POST", "/api/v1/feedback", json_body=body)
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def list_feedback(
        self,
        agent_id: str,
        *,
        after: float,
        before: float | None = None,
        session_id: str | None = None,
        feedback_type: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"agent_id": agent_id, "after": after}
        if before is not None:
            query["before"] = before
        if session_id is not None:
            query["session_id"] = session_id
        if feedback_type is not None:
            query["feedback_type"] = feedback_type
        if q is not None:
            query["q"] = q
        if limit is not None:
            query["limit"] = limit
        response = self._request("GET", "/api/v1/feedback", query=query)
        feedback = response.json()["data"].get("feedback")
        if not isinstance(feedback, list):
            return []
        return [item for item in feedback if isinstance(item, dict)]

    def list_workflows(self, *, host: str | None = None) -> list[dict[str, Any]]:
        query = {"host": host} if host else None
        response = self._request("GET", "/api/v1/workflow", query=query)
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
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/run",
            json_body=body,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def get_workflow_run(self, workflow_id: str, run_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/runs/{quote(run_id, safe='')}",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def stream_workflow_run(
        self, workflow_id: str, run_id: str
    ) -> Iterator[dict[str, Any]]:
        with self._request(
            "GET",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/runs/{quote(run_id, safe='')}/events",
            stream=True,
        ) as response:
            yield from self._iter_sse_events(response)

    def close(self) -> None:
        self._session.close()

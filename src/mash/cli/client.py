"""Remote HTTP client for Mash host deployments.

Request/response calls are synchronous on ``httpx.Client``; the REPL runs them
in worker threads. Event streams are async generators on ``httpx.AsyncClient``
that the REPL's tasks read, and that continue from the last event's seq when
the connection drops.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Iterator, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from mash.runtime.structured_output import serialize_structured_output

DEFAULT_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# No read timeout: a stream is quiet while the request waits on the user.
DEFAULT_STREAM_TIMEOUT = httpx.Timeout(10.0, read=None)
# Compacting a session is a model call.
COMPACT_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
# A browser call may start the user's browser container first.
BROWSER_TIMEOUT = httpx.Timeout(90.0, connect=10.0)

TERMINAL_EVENTS = frozenset({"request.completed", "request.error", "request.cancelled"})

# A dropped event stream is retried this many times, waiting
# STREAM_RETRY_DELAY * 2**attempt seconds (capped) before each try. Any event
# that arrives resets the count.
STREAM_RETRIES = 5
STREAM_RETRY_DELAY = 0.5
STREAM_RETRY_MAX_DELAY = 8.0


class MashHostClientError(RuntimeError):
    """Raised when Mash host operations fail.

    ``status_code`` is the HTTP status, or None when the host was not reached.
    ``code`` is the API's error code, such as ``CORTEX_NOT_FOUND``, when the
    response carried one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class _SSEParser:
    """Turns the lines of an SSE body into ``{"event", "data", "seq"?}`` dicts."""

    def __init__(self) -> None:
        self._event: Optional[str] = None
        self._seq: Optional[int] = None
        self._data: list[str] = []

    def feed(self, line: str) -> Optional[dict[str, Any]]:
        """Take one line; return the event it completes, if any."""
        stripped = line.strip()
        if not stripped:
            return self.flush()
        if stripped.startswith(":"):
            return None
        if stripped.startswith("id:"):
            raw = stripped[3:].strip()
            self._seq = int(raw) if raw.isdigit() else None
        elif stripped.startswith("event:"):
            self._event = stripped[6:].strip()
        elif stripped.startswith("data:"):
            self._data.append(stripped[5:].strip())
        return None

    def flush(self) -> Optional[dict[str, Any]]:
        """The buffered event, if it is complete; the buffer is cleared.

        Also called when the body ends: the server sends the terminal event
        as its last frame and closes, and a line reader may drop the blank
        line that ends that frame.
        """
        event_name, data_lines, seq = self._event, self._data, self._seq
        self._event, self._data, self._seq = None, [], None
        if not (event_name and data_lines):
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        event: dict[str, Any] = {"event": event_name, "data": payload}
        if seq is not None:
            event["seq"] = seq
        return event


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = (
        payload.get("error", {}).get("message") if isinstance(payload, dict) else None
    )
    return message or response.text


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code) if code else None


class MashHostClient:
    """Thin client for interacting with a Mash host deployment."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self._api_key = api_key
        self._headers = {"Accept": "application/json, text/event-stream"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        # Tests pass transports; httpx picks its own otherwise.
        self._async_transport = async_transport
        self._session = httpx.Client(
            headers=self._headers,
            timeout=DEFAULT_REQUEST_TIMEOUT,
            transport=transport,
        )

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parts = urlsplit((base_url or "").strip())
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _raise_for_status(method: str, path: str, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise MashHostClientError(
                f"{method} {path} failed ({response.status_code}): "
                f"{_error_message(response)}",
                status_code=response.status_code,
                code=_error_code(response),
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        try:
            response = self._session.request(
                method,
                self._url(path),
                params=query or None,
                json=json_body,
                timeout=timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise MashHostClientError(f"{method} {path} failed: {exc}") from exc
        self._raise_for_status(method, path, response)
        return response

    def _stream(
        self, path: str, *, query: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Read one SSE response to its end, on the calling thread."""
        parser = _SSEParser()
        try:
            with self._session.stream(
                "GET",
                self._url(path),
                params=query or None,
                timeout=DEFAULT_STREAM_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                self._raise_for_status("GET", path, response)
                for line in response.iter_lines():
                    event = parser.feed(line)
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise MashHostClientError(f"GET {path} failed: {exc}") from exc
        event = parser.flush()
        if event is not None:
            yield event

    async def _astream(
        self, path: str, *, query: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Read one SSE response to its end, on the event loop.

        Transport errors are raised as ``httpx.HTTPError`` for the caller to
        retry; an error status is a ``MashHostClientError``.
        """
        parser = _SSEParser()
        # One client per stream: streams are few and long, and a client is
        # bound to the loop that opened it.
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=DEFAULT_STREAM_TIMEOUT,
            transport=self._async_transport,
        ) as session:
            async with session.stream(
                "GET", self._url(path), params=query or None
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                self._raise_for_status("GET", path, response)
                async for line in response.aiter_lines():
                    event = parser.feed(line)
                    if event is not None:
                        yield event
        event = parser.flush()
        if event is not None:
            yield event

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
        agents: list[str] | None = None,
        workflows: list[str] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            "PUT",
            f"/api/v1/hosts/{quote(host_id, safe='')}",
            json_body={
                "agents": list(agents or []),
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
        user_id: str,
        structured_output: Any = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        json_body: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "user_id": user_id,
        }
        if mode is not None:
            json_body["mode"] = mode
        if structured_output is not None:
            json_body["structured_output"] = serialize_structured_output(structured_output)
        response = self._request(
            "POST",
            f"/api/v1/hosts/{quote(host_id, safe='')}/request",
            json_body=json_body,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def submit_request(
        self,
        agent_id: str,
        *,
        message: str,
        session_id: str,
        user_id: str,
        structured_output: Any = None,
        mode: str | None = None,
    ) -> str:
        json_body: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "user_id": user_id,
        }
        if mode is not None:
            json_body["mode"] = mode
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

    def set_request_mode(
        self, agent_id: str, request_id: str, mode: str
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/mode",
            json_body={"mode": mode},
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def cancel_request(self, agent_id: str, request_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/cancel",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def rerun_request(self, agent_id: str, request_id: str) -> dict[str, Any]:
        """Start a previous request over as a new request; returns its ``request_id``."""
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/rerun",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def compact_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        """Summarize the session's history; returns ``summary_text`` and ``trace_id``."""
        response = self._request(
            "POST",
            f"/api/v1/agent/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}/compact",
            json_body={"reason": "manual"},
            # The summary is a model call.
            timeout=COMPACT_TIMEOUT,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def get_usage(
        self,
        agent_id: str,
        *,
        session_id: str | None = None,
        from_ts: float | None = None,
        bucket: str = "day",
    ) -> list[dict[str, Any]]:
        """The agent's token usage in ``hour`` or ``day`` buckets."""
        query = {
            key: value
            for key, value in (
                ("agent_id", agent_id),
                ("session_id", session_id),
                ("from_ts", from_ts),
                ("bucket", bucket),
            )
            if value is not None
        }
        response = self._request("GET", "/api/v1/telemetry/usage", query=query)
        buckets = response.json()["data"].get("buckets")
        return buckets if isinstance(buckets, list) else []

    def list_tools(self) -> list[dict[str, Any]]:
        """The pool's tools, each as ``{"tool", "agents"}``."""
        tools = self._request("GET", "/api/v1/tools").json()["data"].get("tools")
        return tools if isinstance(tools, list) else []

    def list_skills(self) -> list[dict[str, Any]]:
        """The pool's skills, each as ``{"skill", "agents"}``."""
        skills = self._request("GET", "/api/v1/skills").json()["data"].get("skills")
        return skills if isinstance(skills, list) else []

    # --- Cortex -------------------------------------------------------------

    @staticmethod
    def _cortex_path(user_id: str, *parts: str) -> str:
        path = f"/api/v1/cortex/users/{quote(user_id, safe='')}"
        return path + "".join(f"/{quote(part, safe='')}" for part in parts)

    def get_cortex_user(self, user_id: str) -> dict[str, Any]:
        """The user's Cortex: ``profile``, ``version`` and ``entry_count``."""
        data = self._request("GET", self._cortex_path(user_id)).json()["data"]
        return data if isinstance(data, dict) else {}

    def list_cortex_entries(
        self, user_id: str, *, q: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"limit": limit}
        if q:
            query["q"] = q
        response = self._request(
            "GET", self._cortex_path(user_id, "entries"), query=query
        )
        entries = response.json()["data"].get("entries")
        return entries if isinstance(entries, list) else []

    def create_cortex_entry(
        self, user_id: str, *, topic: str, content: str
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            self._cortex_path(user_id, "entries"),
            json_body={"topic": topic, "content": content},
        )
        entry = response.json()["data"].get("entry")
        return entry if isinstance(entry, dict) else {}

    def delete_cortex_entry(self, user_id: str, entry_id: str) -> None:
        self._request("DELETE", self._cortex_path(user_id, "entries", entry_id))

    # --- browser ------------------------------------------------------------

    def list_browser_sites(self, user_id: str) -> list[dict[str, Any]]:
        """The sites the user's browser profile holds cookies for."""
        response = self._request(
            "GET",
            f"/api/v1/browser/users/{quote(user_id, safe='')}/sites",
            timeout=BROWSER_TIMEOUT,
        )
        sites = response.json()["data"].get("sites")
        return sites if isinstance(sites, list) else []

    def clear_browser_site(self, user_id: str, domain: str) -> dict[str, Any]:
        """Sign the user's browser out of ``domain``."""
        response = self._request(
            "DELETE",
            f"/api/v1/browser/users/{quote(user_id, safe='')}/sites/{quote(domain, safe='')}",
            timeout=BROWSER_TIMEOUT,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def list_session_requests(
        self,
        agent_id: str,
        session_id: str,
        *,
        mode: str | None = None,
        status: str | None = None,
        started_by: str | None = None,
    ) -> list[dict[str, Any]]:
        query = {
            key: value
            for key, value in (
                ("mode", mode),
                ("status", status),
                ("started_by", started_by),
            )
            if value is not None
        }
        response = self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}/requests",
            query=query or None,
        )
        requests_ = response.json()["data"].get("requests")
        if not isinstance(requests_, list):
            return []
        return [item for item in requests_ if isinstance(item, dict)]

    def get_request_status(self, agent_id: str, request_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/request/{quote(request_id, safe='')}/status",
        )
        return response.json()["data"]

    def stream_request(
        self, agent_id: str, request_id: str, *, after: int = 0
    ) -> Iterator[dict[str, Any]]:
        """The request's events after seq ``after``, read on the calling thread."""
        yield from self._stream(
            self._request_events_path(agent_id, request_id),
            query={"after": after} if after else None,
        )

    @staticmethod
    def _request_events_path(agent_id: str, request_id: str) -> str:
        return (
            f"/api/v1/agent/{quote(agent_id, safe='')}"
            f"/request/{quote(request_id, safe='')}/events"
        )

    async def request_events(
        self, agent_id: str, request_id: str, *, after: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the request's events after seq ``after`` until its terminal event.

        When the connection drops, or the stream ends before a terminal
        event, it is opened again after the last seq seen, so no event is
        repeated or lost. It gives up with ``MashHostClientError`` after
        ``STREAM_RETRIES`` tries in a row without an event. A host that sends
        no seq cannot be continued: the generator ends where the stream did.
        """
        path = self._request_events_path(agent_id, request_id)
        cursor = max(0, int(after))
        failures = 0
        while True:
            resumable = True
            error: Exception | None = None
            try:
                async for event in self._astream(
                    path, query={"after": cursor} if cursor else None
                ):
                    failures = 0
                    seq = event.get("seq")
                    if isinstance(seq, int):
                        cursor = seq
                    else:
                        resumable = False
                    yield event
                    if event.get("event") in TERMINAL_EVENTS:
                        return
            except httpx.HTTPError as exc:
                error = exc
            if not resumable:
                if error is not None:
                    raise MashHostClientError(f"GET {path} failed: {error}") from error
                return
            failures += 1
            if failures > STREAM_RETRIES:
                reason = error or "the stream ended before the request did"
                raise MashHostClientError(
                    f"GET {path} failed: {reason}"
                ) from error
            await asyncio.sleep(
                min(STREAM_RETRY_DELAY * 2 ** (failures - 1), STREAM_RETRY_MAX_DELAY)
            )

    def list_sessions(
        self,
        agent_id: str,
        *,
        user_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The agent's sessions, most recently active first; one user's with ``user_id``."""
        query = {
            key: value
            for key, value in (("user_id", user_id), ("limit", limit))
            if value is not None
        }
        response = self._request(
            "GET",
            f"/api/v1/agent/{quote(agent_id, safe='')}/sessions",
            query=query or None,
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
            query={"agent_id": agent_id, "session_id": session_id, "trace_id": trace_id},
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

    def record_command_event(self, event: dict[str, Any]) -> None:
        """Ship one CLI command lifecycle event to the host (best-effort)."""
        body = {k: v for k, v in event.items() if v is not None}
        self._request(
            "POST",
            "/api/v1/telemetry/command-events",
            json_body=body,
            timeout=5.0,
        )

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

    def get_workflow_definition(self, workflow_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if dedup_key is not None:
            body["dedup_key"] = dedup_key
        if workflow_input is not None:
            body["input"] = workflow_input
        if session_id is not None:
            body["session_id"] = session_id
        if user_id is not None:
            body["user_id"] = user_id
        response = self._request(
            "POST",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/run",
            json_body=body,
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def list_workflow_runs(
        self, workflow_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """A workflow's runs, newest first."""
        response = self._request(
            "GET",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/runs",
            query={"limit": limit},
        )
        runs = response.json()["data"].get("runs")
        return runs if isinstance(runs, list) else []

    def get_workflow_run(self, workflow_id: str, run_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/runs/{quote(run_id, safe='')}",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    def resume_workflow_run(self, workflow_id: str, run_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/v1/workflow/{quote(workflow_id, safe='')}/runs/{quote(run_id, safe='')}/resume",
        )
        data = response.json()["data"]
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _workflow_events_path(workflow_id: str, run_id: str) -> str:
        return (
            f"/api/v1/workflow/{quote(workflow_id, safe='')}"
            f"/runs/{quote(run_id, safe='')}/events"
        )

    def stream_workflow_run(
        self, workflow_id: str, run_id: str
    ) -> Iterator[dict[str, Any]]:
        yield from self._stream(self._workflow_events_path(workflow_id, run_id))

    async def workflow_run_events(
        self, workflow_id: str, run_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield a workflow run's events to the end of its stream."""
        path = self._workflow_events_path(workflow_id, run_id)
        try:
            async for event in self._astream(path):
                yield event
        except httpx.HTTPError as exc:
            raise MashHostClientError(f"GET {path} failed: {exc}") from exc

    def close(self) -> None:
        self._session.close()

"""HTTP client for interacting with one MashAgent instance."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests


class MashAgentClientError(RuntimeError):
    """Raised when MashAgentClient operations fail."""


class _CommandEventHTTPLogger:
    """Event logger proxy that forwards command events over HTTP control API."""

    def __init__(self, client: "MashAgentClient") -> None:
        self._client = client

    def emit(self, event: Any) -> None:
        if hasattr(event, "to_dict"):
            payload = event.to_dict()
        elif isinstance(event, dict):
            payload = event
        else:
            return
        if not isinstance(payload, dict):
            return
        try:
            self._client.emit_command_event(payload)
        except MashAgentClientError:
            return


class MashAgentClient:
    """Client maintaining a 1:1 connection with a single MashAgent."""

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

        self._session = requests.Session()
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if default_headers:
            for key, value in default_headers.items():
                if isinstance(key, str) and value is not None:
                    self._headers[key] = str(value)
        self._event_logger = _CommandEventHTTPLogger(self)

    @property
    def app_id(self) -> str:
        """Return application id for this client session."""
        return self.agent_id

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parts = urlsplit((base_url or "").strip())
        path = parts.path.rstrip("/")
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    def _requests_url(self) -> str:
        return f"{self.base_url}/agents/{self.agent_id}/requests"

    def _request_stream_url(self, request_id: str) -> str:
        return f"{self._requests_url()}/{request_id}"

    def _control_url(self) -> str:
        return f"{self.base_url}/agents/{self.agent_id}/control"

    def _control_post(
        self,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = self._session.post(
            self._control_url(),
            headers=self._headers,
            json={
                "action": action,
                "payload": dict(payload or {}),
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise MashAgentClientError(
                f"POST control failed (status={response.status_code}): {response.text}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise MashAgentClientError("control response must be an object")
        return data

    def get_default_session_id(self) -> str:
        payload = self._control_post("get_default_session_id")
        value = str(payload.get("default_session_id") or "").strip()
        if not value:
            raise MashAgentClientError("control response missing default_session_id")
        return value

    def get_event_logger(self) -> _CommandEventHTTPLogger:
        return self._event_logger

    def emit_command_event(self, event_payload: Dict[str, Any]) -> None:
        """Forward one command event to the runtime control API."""
        self._control_post("emit_command_event", {"event": dict(event_payload)})

    def set_chain_renderer(self, renderer: Any) -> None:
        """No-op for transport client. Runtime trace rendering is server-side."""
        del renderer

    def get_subagent_ids(self) -> list[str]:
        payload = self._control_post("get_subagent_ids")
        values = payload.get("subagent_ids")
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if text:
                result.append(text)
        return result

    def set_subagent_ids(self, subagent_ids: Sequence[str]) -> None:
        self._control_post(
            "set_subagent_ids",
            {"subagent_ids": [str(value) for value in subagent_ids]},
        )

    def get_session_info(self, session_id: str | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if session_id is not None:
            payload["session_id"] = session_id
        result = self._control_post("get_session_info", payload)
        if not isinstance(result, dict):
            raise MashAgentClientError("session_info response must be an object")
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        payload = self._control_post("list_sessions")
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            return []
        return [session for session in sessions if isinstance(session, dict)]

    def get_model(self) -> str:
        info = self.get_session_info()
        return str(info.get("model") or "")

    def get_max_steps(self) -> int:
        info = self.get_session_info()
        try:
            return int(info.get("max_steps", 0))
        except (TypeError, ValueError):
            return 0

    def get_session_total_tokens(self, session_id: str | None = None) -> int:
        info = self.get_session_info(session_id)
        try:
            return int(info.get("session_total_tokens", 0))
        except (TypeError, ValueError):
            return 0

    def get_preferences(self, session_id: str) -> Optional[Dict[str, Any]]:
        payload = self._control_post("get_preferences", {"session_id": session_id})
        value = payload.get("preferences")
        return value if isinstance(value, dict) else None

    def get_latest_preferences(self) -> Optional[Dict[str, Any]]:
        payload = self._control_post("get_latest_preferences")
        value = payload.get("preferences")
        return value if isinstance(value, dict) else None

    def set_preferences(self, session_id: str, preferences: Dict[str, Any]) -> None:
        self._control_post(
            "set_preferences",
            {"session_id": session_id, "preferences": dict(preferences)},
        )

    def list_app_data(self, session_id: str) -> list[dict[str, Any]]:
        payload = self._control_post("list_app_data", {"session_id": session_id})
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def get_app_data(self, session_id: str, key: str) -> Any:
        payload = self._control_post("get_app_data", {"session_id": session_id, "key": key})
        return payload.get("value")

    def set_app_data(self, session_id: str, key: str, value: Any) -> None:
        self._control_post(
            "set_app_data",
            {"session_id": session_id, "key": key, "value": value},
        )

    def delete_app_data(self, session_id: str, key: str) -> bool:
        payload = self._control_post("delete_app_data", {"session_id": session_id, "key": key})
        return bool(payload.get("deleted"))

    def get_history_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        payload: Dict[str, Any] = {"session_id": session_id}
        if limit is not None:
            payload["limit"] = int(limit)
        result = self._control_post("get_history_turns", payload)
        turns = result.get("turns")
        if not isinstance(turns, list):
            return []
        return [turn for turn in turns if isinstance(turn, dict)]

    def compact_session(
        self,
        session_id: str | None = None,
        *,
        reason: str = "manual",
        session_total_tokens_reset: int = 0,
    ) -> tuple[Optional[str], Optional[str]]:
        payload: Dict[str, Any] = {
            "reason": reason,
            "session_total_tokens_reset": int(session_total_tokens_reset),
        }
        if session_id is not None:
            payload["session_id"] = session_id
        result = self._control_post("compact_session", payload)
        summary_text = result.get("summary_text")
        turn_id = result.get("turn_id")
        return (
            summary_text if isinstance(summary_text, str) else None,
            turn_id if isinstance(turn_id, str) else None,
        )

    def post_request(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "message": message,
            "turn_metadata": dict(turn_metadata or {}),
        }
        if session_id is not None:
            payload["session_id"] = session_id

        response = self._session.post(
            self._requests_url(),
            headers=self._headers,
            json=payload,
            timeout=30,
        )
        if response.status_code != 202:
            raise MashAgentClientError(
                f"POST request failed (status={response.status_code}): {response.text}"
            )

        data = response.json()
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            raise MashAgentClientError("Agent POST response missing request_id")
        return request_id

    def stream(self, request_id: str, *, timeout: Optional[float] = None) -> Iterator[Dict[str, Any]]:
        headers = dict(self._headers)
        headers["Accept"] = "text/event-stream"

        kwargs: Dict[str, Any] = {
            "headers": headers,
            "stream": True,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout

        with self._session.get(self._request_stream_url(request_id), **kwargs) as response:
            if response.status_code != 200:
                raise MashAgentClientError(
                    f"GET stream failed (status={response.status_code}): {response.text}"
                )

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

    def invoke(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        request_id = self.post_request(
            message,
            session_id=session_id,
            turn_metadata=turn_metadata,
        )

        started = time.time()
        timeout_seconds = None if timeout_ms is None else max(1, int(timeout_ms)) / 1000.0
        stream_timeout = timeout_seconds if timeout_seconds is not None else None

        try:
            for event in self.stream(request_id, timeout=stream_timeout):
                event_name = str(event.get("event") or "")
                data = event.get("data")
                if event_name == "request.completed":
                    if not isinstance(data, dict):
                        raise MashAgentClientError("request.completed payload is invalid")
                    return data
                if event_name == "request.error":
                    error_message = "request failed"
                    if isinstance(data, dict) and isinstance(data.get("error"), str):
                        error_message = data["error"]
                    raise MashAgentClientError(error_message)
                if timeout_seconds is not None and time.time() - started > timeout_seconds:
                    raise TimeoutError("agent invoke timed out")
        except requests.exceptions.RequestException as exc:
            if timeout_seconds is not None and "timed out" in str(exc).lower():
                raise TimeoutError("agent invoke timed out") from exc
            raise MashAgentClientError(f"agent invoke failed: {exc}") from exc

        raise MashAgentClientError("SSE stream ended without a terminal event")

    def close(self) -> None:
        self._session.close()


__all__ = ["MashAgentClient", "MashAgentClientError"]

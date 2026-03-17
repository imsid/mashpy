"""Tool for invoking subagents through a client interface."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterator, Optional, Protocol

from ..logging import AgentTraceEvent, get_trace_id
from ..runtime.session import derive_subagent_session_id
from .base import ToolResult

DEFAULT_SUBAGENT_TIMEOUT_MS = 360_000


class SupportsSubagentStream(Protocol):
    """Client protocol for host-managed subagent request streaming."""

    def post_request(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit one subagent request and return its request id."""

    def stream(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield streamed request events for a submitted subagent request."""


class SupportsEventEmit(Protocol):
    """Event logger protocol used for forwarding subagent stream events."""

    def emit(self, event: AgentTraceEvent) -> None:
        """Emit one trace event."""


class InvokeSubagentTool:
    """Tool entrypoint used by primary agents to call subagents."""

    name = "InvokeSubagent"
    description = (
        "Invoke a subagent by id with a focused prompt. "
        "Use this when a specialized subagent can help with the task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Subagent identifier to invoke.",
            },
            "prompt": {
                "type": "string",
                "description": "Prompt sent to the subagent.",
            },
            "opts": {
                "type": "object",
                "description": (
                    "Optional invocation options, e.g. {\"timeout_ms\": 30000}. "
                    f"Defaults to {DEFAULT_SUBAGENT_TIMEOUT_MS} ms."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["agent_id", "prompt"],
    }

    def __init__(
        self,
        *,
        primary_app_id: str,
        client_resolver: Callable[[str], SupportsSubagentStream],
        primary_session_id_provider: Optional[Callable[[], str]] = None,
        primary_session_id: Optional[str] = None,
        event_logger: Optional[SupportsEventEmit] = None,
    ) -> None:
        if client_resolver is None:
            raise ValueError("client_resolver is required")
        self._client_resolver = client_resolver
        self._primary_app_id = primary_app_id
        self._event_logger = event_logger
        if primary_session_id_provider is not None:
            self._primary_session_id_provider = primary_session_id_provider
        elif primary_session_id:
            self._primary_session_id_provider = lambda: primary_session_id
        else:
            raise ValueError("primary_session_id or primary_session_id_provider is required")

    def _emit_stream_event(
        self,
        *,
        primary_session_id: str,
        subagent_session_id: str,
        agent_id: str,
        request_id: str,
        event_name: str,
        data: Dict[str, Any],
    ) -> None:
        if self._event_logger is None:
            return

        self._event_logger.emit(
            AgentTraceEvent(
                event_type=f"subagent.{event_name}",
                app_id=self._primary_app_id,
                session_id=primary_session_id,
                trace_id=get_trace_id(),
                payload={
                    "agent_id": agent_id,
                    "primary_session_id": primary_session_id,
                    "subagent_session_id": subagent_session_id,
                    "request_id": request_id,
                    "event": event_name,
                    "data": dict(data),
                },
            )
        )

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        agent_id = str(args.get("agent_id", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        opts = args.get("opts") or {}
        if not agent_id:
            return ToolResult.error("agent_id is required")
        if not prompt:
            return ToolResult.error("prompt is required")
        if not isinstance(opts, dict):
            return ToolResult.error("opts must be an object")

        primary_session_id = self._primary_session_id_provider().strip()
        if not primary_session_id:
            return ToolResult.error("primary_session_id is required")
        subagent_session_id = derive_subagent_session_id(
            self._primary_app_id,
            primary_session_id,
            agent_id,
        )

        started_at = time.time()
        try:
            timeout_ms: Optional[int] = DEFAULT_SUBAGENT_TIMEOUT_MS
            timeout_value = opts.get("timeout_ms")
            if timeout_value is not None:
                try:
                    timeout_ms = int(timeout_value)
                except (TypeError, ValueError):
                    timeout_ms = DEFAULT_SUBAGENT_TIMEOUT_MS
                if timeout_ms is not None and timeout_ms <= 0:
                    timeout_ms = DEFAULT_SUBAGENT_TIMEOUT_MS

            client = self._client_resolver(agent_id)
            request_id = client.post_request(
                prompt,
                session_id=subagent_session_id,
                turn_metadata={
                    "primary_session_id": primary_session_id,
                    "primary_app_id": self._primary_app_id,
                    "subagent_id": agent_id,
                    "subagent_invoke_opts": dict(opts),
                },
            )
            timeout_seconds = None if timeout_ms is None else max(1, int(timeout_ms)) / 1000.0

            result: Optional[Dict[str, Any]] = None
            for event in client.stream(request_id, timeout=timeout_seconds):
                event_name = str(event.get("event") or "")
                data = event.get("data")
                if not isinstance(data, dict):
                    data = {}

                if event_name:
                    self._emit_stream_event(
                        primary_session_id=primary_session_id,
                        subagent_session_id=subagent_session_id,
                        agent_id=agent_id,
                        request_id=request_id,
                        event_name=event_name,
                        data=data,
                    )

                if event_name == "request.completed":
                    result = data
                    break
                if event_name == "request.error":
                    error_message = str(data.get("error") or "request failed")
                    raise RuntimeError(error_message)
                if timeout_seconds is not None and time.time() - started_at > timeout_seconds:
                    raise TimeoutError("agent invoke timed out")

            if result is None:
                raise RuntimeError("subagent stream ended without a terminal event")

            response = result.get("response")
            if isinstance(response, dict):
                text = response.get("text", "")
                metadata = response.get("metadata", {})
            else:
                text = result.get("text", "")
                metadata = result.get("metadata", {})
        except Exception as exc:
            payload = {
                "agent_id": agent_id,
                "session_id": primary_session_id,
                "primary_app_id": self._primary_app_id,
                "error": str(exc),
                "duration_ms": int((time.time() - started_at) * 1000),
            }
            return ToolResult.error(json.dumps(payload, ensure_ascii=True))

        payload = {
            "agent_id": agent_id,
            "session_id": primary_session_id,
            "subagent_session_id": subagent_session_id,
            "primary_app_id": self._primary_app_id,
            "request_id": result.get("request_id"),
            "text": text,
            "metadata": metadata,
            "duration_ms": int((time.time() - started_at) * 1000),
        }
        return ToolResult.success(
            json.dumps(payload, ensure_ascii=True),
            agent_id=agent_id,
            primary_session_id=primary_session_id,
            subagent_session_id=subagent_session_id,
            request_id=result.get("request_id"),
        )

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

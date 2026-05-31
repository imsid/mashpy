"""Tool for invoking subagents through a client interface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, AsyncIterator, Callable, Dict, Optional, Protocol

from ..logging import AgentTraceEvent, get_trace_id
from ..runtime.errors import classify_error
from .base import ToolResult

DEFAULT_SUBAGENT_TIMEOUT_MS = 360_000
MAX_STEP_LIMIT_PREFIX = "Stopped after reaching the max step limit"


def derive_subagent_session_id(
    primary_app_id: str,
    primary_session_id: str,
    subagent_id: str,
) -> str:
    """Derive deterministic subagent session namespace from primary context."""
    key = f"{primary_app_id}:{primary_session_id}:{subagent_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"subagent:{subagent_id}:{digest}"


class SupportsSubagentStream(Protocol):
    """Client protocol for host-managed subagent request streaming."""

    async def post_subagent_request(
        self,
        message: str,
        *,
        session_id: str,
        primary_session_id: str,
        primary_app_id: str,
        subagent_id: str,
        subagent_invoke_opts: Dict[str, Any],
    ) -> str:
        """Submit one subagent request with parent-call context."""

    def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield streamed request events for a submitted subagent request."""


class SupportsEventEmit(Protocol):
    """Event logger protocol used for forwarding subagent stream events."""

    async def emit(self, event: AgentTraceEvent) -> None:
        """Emit one trace event."""


class InvokeSubagentTool:
    """Tool entrypoint used by primary agents to call subagents."""

    name = "InvokeSubagent"
    requires_approval = False
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

    async def _emit_stream_event(
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
        trace_id = get_trace_id()
        if not trace_id:
            return

        try:
            await self._event_logger.emit(
                AgentTraceEvent(
                    event_type=f"subagent.{event_name}",
                    app_id=self._primary_app_id,
                    session_id=primary_session_id,
                    trace_id=trace_id,
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
        except Exception:
            # Subagent telemetry should never change tool behavior.
            return

    def _error_result(
        self,
        *,
        agent_id: str,
        primary_session_id: str,
        subagent_session_id: str,
        started_at: float,
        request_id: Optional[str],
        error: object,
        error_source: str,
    ) -> ToolResult:
        payload = {
            "agent_id": agent_id,
            "primary_session_id": primary_session_id,
            "subagent_session_id": subagent_session_id,
            "request_id": request_id,
            "error_source": error_source,
            "duration_ms": int((time.time() - started_at) * 1000),
            **classify_error(error),
        }
        return ToolResult.error(
            json.dumps(payload, ensure_ascii=True),
            agent_id=agent_id,
            primary_session_id=primary_session_id,
            subagent_session_id=subagent_session_id,
            request_id=request_id,
            error_source=error_source,
            error_code=payload.get("error_code"),
            retryable=payload.get("retryable"),
        )

    def _response_error_result(
        self,
        *,
        agent_id: str,
        primary_session_id: str,
        subagent_session_id: str,
        started_at: float,
        request_id: Optional[str],
        error: str,
        error_code: str,
    ) -> ToolResult:
        payload = {
            "agent_id": agent_id,
            "primary_session_id": primary_session_id,
            "subagent_session_id": subagent_session_id,
            "request_id": request_id,
            "error_source": "subagent_response",
            "error_code": error_code,
            "retryable": False,
            "duration_ms": int((time.time() - started_at) * 1000),
            "error": error,
        }
        return ToolResult.error(
            json.dumps(payload, ensure_ascii=True),
            agent_id=agent_id,
            primary_session_id=primary_session_id,
            subagent_session_id=subagent_session_id,
            request_id=request_id,
            error_source="subagent_response",
            error_code=error_code,
            retryable=False,
        )

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
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
        request_id: Optional[str] = None
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
            if not callable(getattr(client, "post_subagent_request", None)):
                raise RuntimeError("subagent client does not support subagent request submission")
            if not callable(getattr(client, "stream_response", None)):
                raise RuntimeError("subagent client does not support request streaming")

            async def _invoke() -> Dict[str, Any] | None:
                nonlocal request_id
                request_id = await client.post_subagent_request(
                    prompt,
                    session_id=subagent_session_id,
                    primary_session_id=primary_session_id,
                    primary_app_id=self._primary_app_id,
                    subagent_id=agent_id,
                    subagent_invoke_opts=dict(opts),
                )
                timeout_seconds = None if timeout_ms is None else max(1, int(timeout_ms)) / 1000.0

                result: Optional[Dict[str, Any]] = None
                async for event in client.stream_response(request_id, timeout=timeout_seconds):
                    event_name = str(event.get("event") or "")
                    data = event.get("data")
                    if not isinstance(data, dict):
                        data = {}

                    if event_name:
                        await self._emit_stream_event(
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
                        error_payload = classify_error(data.get("error") or "request failed")
                        if data.get("error_code") is not None:
                            error_payload["error_code"] = data.get("error_code")
                        if data.get("retryable") is not None:
                            error_payload["retryable"] = data.get("retryable")
                        raise RuntimeError(
                            json.dumps(
                                {
                                    "request_id": data.get("request_id", request_id),
                                    **error_payload,
                                },
                                ensure_ascii=True,
                            )
                        )
                    if timeout_seconds is not None and time.time() - started_at > timeout_seconds:
                        raise TimeoutError("agent invoke timed out")
                return result

            result = await _invoke()

            if result is None:
                return self._error_result(
                    agent_id=agent_id,
                    primary_session_id=primary_session_id,
                    subagent_session_id=subagent_session_id,
                    started_at=started_at,
                    request_id=request_id,
                    error="subagent stream ended without a terminal event",
                    error_source="stream",
                )

            response = result.get("response")
            if isinstance(response, dict):
                text = response.get("text", "")
                metadata = response.get("metadata", {})
            else:
                text = result.get("text", "")
                metadata = result.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                try:
                    parsed = json.loads(str(exc))
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and "error" in parsed:
                    payload = {
                        "agent_id": agent_id,
                        "primary_session_id": primary_session_id,
                        "subagent_session_id": subagent_session_id,
                        "request_id": parsed.get("request_id", request_id),
                        "error_source": "subagent",
                        "duration_ms": int((time.time() - started_at) * 1000),
                        **classify_error(parsed.get("error")),
                    }
                    if parsed.get("error_code") is not None:
                        payload["error_code"] = parsed.get("error_code")
                    if parsed.get("retryable") is not None:
                        payload["retryable"] = parsed.get("retryable")
                    return ToolResult.error(
                        json.dumps(payload, ensure_ascii=True),
                        agent_id=agent_id,
                        primary_session_id=primary_session_id,
                        subagent_session_id=subagent_session_id,
                        request_id=payload.get("request_id"),
                        error_source="subagent",
                        error_code=payload.get("error_code"),
                        retryable=payload.get("retryable"),
                    )

            return self._error_result(
                agent_id=agent_id,
                primary_session_id=primary_session_id,
                subagent_session_id=subagent_session_id,
                started_at=started_at,
                request_id=request_id,
                error=exc,
                error_source="timeout" if isinstance(exc, TimeoutError) else "subagent",
            )

        payload = {
            "agent_id": agent_id,
            "primary_session_id": primary_session_id,
            "subagent_session_id": subagent_session_id,
            "primary_app_id": self._primary_app_id,
            "request_id": result.get("request_id"),
            "text": text,
            "metadata": metadata,
            "duration_ms": int((time.time() - started_at) * 1000),
        }
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return self._response_error_result(
                agent_id=agent_id,
                primary_session_id=primary_session_id,
                subagent_session_id=subagent_session_id,
                started_at=started_at,
                request_id=result.get("request_id"),
                error="subagent returned an empty response",
                error_code="empty_response",
            )
        if normalized_text.startswith(MAX_STEP_LIMIT_PREFIX):
            return self._response_error_result(
                agent_id=agent_id,
                primary_session_id=primary_session_id,
                subagent_session_id=subagent_session_id,
                started_at=started_at,
                request_id=result.get("request_id"),
                error=normalized_text,
                error_code="max_steps_exceeded",
            )
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

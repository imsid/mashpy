"""Tool for invoking subagents through a client interface."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Protocol

from ..runtime.session import derive_subagent_session_id
from .base import ToolResult


class SupportsSubagentInvoke(Protocol):
    """Client protocol for host-managed subagent invocation."""

    def invoke(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        turn_metadata: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Invoke one subagent request and wait for terminal result."""


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
                    "Optional invocation options, e.g. {\"timeout_ms\": 30000}."
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
        client_resolver: Callable[[str], SupportsSubagentInvoke],
        primary_session_id_provider: Optional[Callable[[], str]] = None,
        primary_session_id: Optional[str] = None,
    ) -> None:
        if client_resolver is None:
            raise ValueError("client_resolver is required")
        self._client_resolver = client_resolver
        self._primary_app_id = primary_app_id
        if primary_session_id_provider is not None:
            self._primary_session_id_provider = primary_session_id_provider
        elif primary_session_id:
            self._primary_session_id_provider = lambda: primary_session_id
        else:
            raise ValueError("primary_session_id or primary_session_id_provider is required")

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
            timeout_ms: Optional[int] = None
            timeout_value = opts.get("timeout_ms")
            if timeout_value is not None:
                try:
                    timeout_ms = int(timeout_value)
                except (TypeError, ValueError):
                    timeout_ms = None
                if timeout_ms is not None and timeout_ms <= 0:
                    timeout_ms = None

            client = self._client_resolver(agent_id)
            result = client.invoke(
                prompt,
                session_id=subagent_session_id,
                turn_metadata={
                    "primary_session_id": primary_session_id,
                    "primary_app_id": self._primary_app_id,
                    "subagent_id": agent_id,
                    "subagent_invoke_opts": dict(opts),
                },
                timeout_ms=timeout_ms,
            )
            if isinstance(result.get("response"), dict):
                response = result["response"]
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
        return ToolResult.success(json.dumps(payload, ensure_ascii=True))

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

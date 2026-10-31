"""RunSubAgents: run child requests at the same time and wait for all of them."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, Optional, Protocol

from ..logging import (
    AgentTraceEvent,
    get_request_id,
    get_request_metadata,
    get_subagent_id_prefix,
    get_trace_id,
)
from ..runtime.errors import classify_error
from .base import ToolResult

DEFAULT_TIMEOUT_SECONDS = 360

# Child ids are UUID5s in this namespace, so the REPL's 8-character short ids
# stay distinct; a ``{request_id}-sub-...`` string would share the parent's
# prefix.
CHILD_REQUEST_NAMESPACE = uuid.UUID("6f0d3b1e-8c47-4f53-9a8e-2b5d1c7e4a90")


def child_request_id(prefix: str, index: int) -> str:
    """The id of the ``index``-th child of the call bound to ``prefix``."""
    return str(uuid.uuid5(CHILD_REQUEST_NAMESPACE, f"{prefix}-{index}"))


class SupportsChildClient(Protocol):
    """The H2A client operations RunSubAgents uses for one child."""

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        structured_output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        host: Optional[Dict[str, Any]] = None,
        parent: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
    ) -> str:
        """Submit one request; a known ``request_id`` returns the existing one."""

    def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield the child's streamed events."""

    async def cancel_request(self, request_id: str) -> Dict[str, Any]:
        """Cancel the child."""


class SupportsEventEmit(Protocol):
    """Event logger protocol used for forwarding child stream events."""

    async def emit(self, event: AgentTraceEvent) -> None:
        """Emit one trace event."""


def _child_error(data: Dict[str, Any]) -> Dict[str, Any]:
    error = classify_error(data.get("error") or "request failed")
    for key in ("error_code", "retryable"):
        if data.get(key) is not None:
            error[key] = data.get(key)
    return error


class RunSubAgentsTool:
    """Start one or more ``bg`` child requests and return when all are done."""

    name = "RunSubAgents"
    requires_approval = False
    # The concurrency is inside the one call.
    parallel_safe = False
    description = (
        "Run one or more requests at the same time, each on this agent or one "
        "of its subagents, and wait for all of them. Use one item to hand a "
        "task to a subagent; use several to split a large task into parts that "
        "run at once, then write one answer from their results. Returns one "
        "entry per item, in order, with the child's request_id, its status "
        "(completed, failed, cancelled or timed_out), and its response, "
        "structured output or error."
    )
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "This agent's own id, or a subagent of the host.",
                        },
                        "prompt": {"type": "string"},
                        "output_schema": {
                            "type": "object",
                            "description": "Optional JSON schema for the child's structured output.",
                        },
                    },
                    "required": ["agent_id", "prompt"],
                },
            },
            "timeout_seconds": {
                "type": "integer",
                "description": f"Defaults to {DEFAULT_TIMEOUT_SECONDS}.",
            },
        },
        "required": ["requests"],
    }

    def __init__(
        self,
        *,
        caller_agent_id: str,
        client_resolver: Callable[[str], SupportsChildClient],
        session_id: str,
        host: Optional[Dict[str, Any]] = None,
        event_logger: Optional[SupportsEventEmit] = None,
    ) -> None:
        if client_resolver is None:
            raise ValueError("client_resolver is required")
        if not str(session_id or "").strip():
            raise ValueError("session_id is required")
        self._caller_agent_id = caller_agent_id
        # The model names this agent in an item to run part of the work itself.
        self.description = (
            f"{type(self).description} Your own agent_id is '{caller_agent_id}'."
        )
        self._client_resolver = client_resolver
        self._session_id = str(session_id).strip()
        self._host = dict(host) if host is not None else None
        self._event_logger = event_logger

    async def _emit_stream_event(
        self,
        *,
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
                    app_id=self._caller_agent_id,
                    session_id=self._session_id,
                    trace_id=trace_id,
                    payload={
                        "agent_id": agent_id,
                        "primary_session_id": self._session_id,
                        "subagent_session_id": self._session_id,
                        "request_id": request_id,
                        "event": event_name,
                        "data": dict(data),
                    },
                )
            )
        except Exception:
            # Child telemetry should never change tool behavior.
            return

    @staticmethod
    def _parse(args: Dict[str, Any]) -> tuple[list[Dict[str, Any]], int] | str:
        requests = args.get("requests")
        if not isinstance(requests, list) or not requests:
            return "requests must be a non-empty array"
        items: list[Dict[str, Any]] = []
        for index, item in enumerate(requests):
            if not isinstance(item, dict):
                return f"requests[{index}] must be an object"
            agent_id = str(item.get("agent_id") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            output_schema = item.get("output_schema")
            if not agent_id:
                return f"requests[{index}].agent_id is required"
            if not prompt:
                return f"requests[{index}].prompt is required"
            if output_schema is not None and not isinstance(output_schema, dict):
                return f"requests[{index}].output_schema must be an object"
            items.append(
                {"agent_id": agent_id, "prompt": prompt, "output_schema": output_schema}
            )
        timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout, bool):
            return "timeout_seconds must be a positive integer"
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "timeout_seconds must be a positive integer"
        if timeout <= 0:
            return "timeout_seconds must be a positive integer"
        return items, timeout

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        parsed = self._parse(args)
        if isinstance(parsed, str):
            return ToolResult.error(parsed)
        items, timeout_seconds = parsed

        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        caller_request_id = get_request_id() or ""
        # The parent request's caller metadata is bound for its run; forward it
        # so the children inherit the context.
        inherited_metadata = get_request_metadata()
        parent = {
            "kind": "subagent",
            "request_id": caller_request_id,
            "agent_id": self._caller_agent_id,
            "session_id": self._session_id,
            "started_by": self._caller_agent_id,
            "parent_request_id": caller_request_id,
        }
        # The durable loop binds a prefix fixed by this call's position, so a
        # replayed or retried call resubmits the same ids and gets back the
        # children already running instead of starting new ones.
        prefix = get_subagent_id_prefix()

        entries: list[Dict[str, Any]] = [{} for _ in items]
        submitted: list[tuple[int, Any, str]] = []
        for index, item in enumerate(items):
            agent_id = item["agent_id"]
            entry: Dict[str, Any] = {"agent_id": agent_id, "request_id": None}
            entries[index] = entry
            try:
                client = self._client_resolver(agent_id)
                request_id = await client.post_request(
                    item["prompt"],
                    session_id=self._session_id,
                    structured_output=item["output_schema"],
                    metadata=inherited_metadata or None,
                    request_id=(
                        child_request_id(prefix, index) if prefix else None
                    ),
                    host=self._host,
                    parent=parent,
                    mode="bg",
                )
            except Exception as exc:
                entry.update({"status": "failed", **classify_error(exc)})
                continue
            entry["request_id"] = request_id
            submitted.append((index, client, request_id))

        await asyncio.gather(
            *(
                self._wait(entries[index], client, request_id, deadline)
                for index, client, request_id in submitted
            )
        )

        for entry in entries:
            entry.setdefault("status", "failed")
        agent_ids = list(dict.fromkeys(item["agent_id"] for item in items))
        request_ids = [entry.get("request_id") for entry in entries]
        return ToolResult.success(
            json.dumps(entries, ensure_ascii=True),
            agent_id=", ".join(agent_ids),
            agent_ids=agent_ids,
            primary_session_id=self._session_id,
            subagent_session_id=self._session_id,
            request_id=request_ids[0] if len(request_ids) == 1 else None,
            request_ids=request_ids,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )

    async def _wait(
        self,
        entry: Dict[str, Any],
        client: Any,
        request_id: str,
        deadline: float,
    ) -> None:
        """Follow one child's stream into ``entry`` until it ends or times out."""
        try:
            await asyncio.wait_for(
                self._follow(entry, client, request_id),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            entry.update(
                {
                    "status": "timed_out",
                    "error": "the child did not finish before timeout_seconds",
                }
            )
            try:
                await client.cancel_request(request_id)
            except Exception:
                pass
        except Exception as exc:
            entry.update({"status": "failed", **classify_error(exc)})
        if "status" not in entry:
            entry.update(
                {
                    "status": "failed",
                    "error": "the child's stream ended without a terminal event",
                }
            )

    async def _follow(self, entry: Dict[str, Any], client: Any, request_id: str) -> None:
        agent_id = entry["agent_id"]
        async for event in client.stream_response(request_id):
            event_name = str(event.get("event") or "")
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}
            if event_name:
                await self._emit_stream_event(
                    agent_id=agent_id,
                    request_id=request_id,
                    event_name=event_name,
                    data=data,
                )
            if event_name == "request.completed":
                response = data.get("response")
                response = response if isinstance(response, dict) else {}
                metadata = response.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                entry["status"] = "completed"
                entry["response"] = str(response.get("text") or "")
                if metadata.get("structured_output") is not None:
                    entry["structured_output"] = metadata["structured_output"]
                return
            if event_name == "request.cancelled":
                entry["status"] = "cancelled"
                return
            if event_name == "request.error":
                entry.update({"status": "failed", **_child_error(data)})
                return

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

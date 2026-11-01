"""Build the events a request writes, for projection, analysis and export tests."""

from __future__ import annotations

import itertools
from typing import Any

from mash.runtime.events.types import RuntimeEvent, RuntimeEventType

_EVENT_IDS = itertools.count(1)


class RequestEvents:
    """Appends one request's events in the shapes the runtime writes them."""

    def __init__(
        self,
        request_id: str = "req-1",
        *,
        agent_id: str = "assistant",
        trace_id: str = "11111111-2222-3333-4444-555555555555",
        session_id: str = "s-1",
        parent_request_id: str | None = None,
        traceparent: str | None = None,
        workflow_run_id: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.agent_id = agent_id
        self.trace_id = trace_id
        self.session_id = session_id
        self.parent_request_id = parent_request_id
        self.traceparent = traceparent
        self.workflow_run_id = workflow_run_id
        self.events: list[RuntimeEvent] = []

    def _add(
        self,
        event_type: str,
        at: float,
        payload: dict[str, Any] | None = None,
        *,
        loop_index: int | None = None,
    ) -> "RequestEvents":
        self.events.append(
            RuntimeEvent(
                event_id=next(_EVENT_IDS),
                request_id=self.request_id,
                request_seq=len(self.events) + 1,
                app_id=self.agent_id,
                agent_id=self.agent_id,
                trace_id=self.trace_id,
                session_id=self.session_id,
                workflow_run_id=self.workflow_run_id,
                event_type=event_type,
                loop_index=loop_index,
                payload=payload or {},
                created_at=at,
            )
        )
        return self

    def accepted(self, at: float = 100.0, message: str = "hello") -> "RequestEvents":
        payload: dict[str, Any] = {
            "message": message,
            "mode": "fg",
            "parent_request_id": self.parent_request_id,
            "attributes": {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.id": self.agent_id,
                "gen_ai.agent.name": self.agent_id,
                "gen_ai.conversation.id": self.session_id,
                "mash.request_id": self.request_id,
                "mash.request.mode": "fg",
            },
        }
        if self.traceparent:
            payload["traceparent"] = self.traceparent
        return self._add(RuntimeEventType.INVOKE_AGENT_START.value, at, payload)

    def context(self, start: float, end: float) -> "RequestEvents":
        self._add(RuntimeEventType.CONTEXT_LOAD_START.value, start)
        return self._add(RuntimeEventType.CONTEXT_LOAD_END.value, end, {"status": "ok"})

    def chat_start(self, loop: int, at: float, model: str = "claude-test") -> "RequestEvents":
        return self._add(
            RuntimeEventType.CHAT_START.value,
            at,
            {"attributes": _chat_attributes(model)},
            loop_index=loop,
        )

    def chat_end(
        self,
        loop: int,
        at: float,
        *,
        model: str = "claude-test",
        tokens: tuple[int, int] = (10, 5),
        cache: tuple[int, int] = (0, 0),
        tool_calls: list[dict[str, Any]] | None = None,
        text: str = "",
        finish: str = "end_turn",
    ) -> "RequestEvents":
        attributes = _chat_attributes(model)
        attributes.update(
            {
                "gen_ai.usage.input_tokens": tokens[0],
                "gen_ai.usage.output_tokens": tokens[1],
                "gen_ai.usage.cache_read.input_tokens": cache[0],
                "gen_ai.usage.cache_creation.input_tokens": cache[1],
                "gen_ai.response.finish_reasons": [finish],
            }
        )
        return self._add(
            RuntimeEventType.CHAT_END.value,
            at,
            {
                "status": "ok",
                "action_type": "tool_call" if tool_calls else "response",
                "assistant_text": text,
                "tool_calls": tool_calls or [],
                "stop_reason": finish,
                "attributes": attributes,
            },
            loop_index=loop,
        )

    def chat(self, loop: int, start: float, end: float, **kw: Any) -> "RequestEvents":
        return self.chat_start(loop, start, kw.get("model", "claude-test")).chat_end(
            loop, end, **kw
        )

    def tool_start(
        self,
        loop: int,
        call_id: str,
        name: str,
        at: float,
        *,
        arguments: dict[str, Any] | None = None,
        subagents: list[dict[str, str]] | None = None,
    ) -> "RequestEvents":
        payload: dict[str, Any] = {
            "arguments": arguments or {},
            "attributes": _tool_attributes(call_id, name),
        }
        if subagents is not None:
            payload["subagents"] = subagents
        return self._add(RuntimeEventType.EXECUTE_TOOL_START.value, at, payload, loop_index=loop)

    def tool_end(
        self, loop: int, call_id: str, name: str, at: float, *, error: bool = False,
        content: str = "ok",
    ) -> "RequestEvents":
        attributes = _tool_attributes(call_id, name)
        if error:
            attributes["error.type"] = "tool_error"
        return self._add(
            RuntimeEventType.EXECUTE_TOOL_END.value,
            at,
            {
                "status": "error" if error else "ok",
                "duration_ms": None,
                "result": {"content": content, "is_error": error},
                "attributes": attributes,
            },
            loop_index=loop,
        )

    def tool(self, loop: int, call_id: str, name: str, start: float, end: float, **kw: Any) -> "RequestEvents":
        error = kw.pop("error", False)
        content = kw.pop("content", "ok")
        self.tool_start(loop, call_id, name, start, **kw)
        return self.tool_end(loop, call_id, name, end, error=error, content=content)

    def step_end(self, loop: int, at: float) -> "RequestEvents":
        return self._add(
            RuntimeEventType.STEP_END.value,
            at,
            {"status": "ok", "attributes": {"mash.loop_index": loop}},
            loop_index=loop,
        )

    def interaction(
        self, interaction_id: str, kind: str, start: float, end: float | None = None
    ) -> "RequestEvents":
        self._add(
            RuntimeEventType.INTERACTION_START.value,
            start,
            {
                "interaction_id": interaction_id,
                "type": kind,
                "attributes": {"mash.interaction.type": kind},
            },
        )
        if end is not None:
            self._add(
                RuntimeEventType.INTERACTION_END.value,
                end,
                {"status": "ok", "interaction_id": interaction_id, "response": "approve"},
            )
        return self

    def end(self, at: float, status: str = "ok", *, error: str | None = None) -> "RequestEvents":
        payload: dict[str, Any] = {"status": status}
        if status == "error":
            payload["error"] = error or "boom"
            payload["error_type"] = "RuntimeError"
            payload["attributes"] = {"error.type": "RuntimeError"}
        if status == "cancelled":
            payload["attributes"] = {"mash.request.status": "cancelled"}
        return self._add(RuntimeEventType.INVOKE_AGENT_END.value, at, payload)

    def resumed(self, at: float) -> "RequestEvents":
        return self._add(
            RuntimeEventType.REQUEST_RESUMED.value,
            at,
            {"attributes": {"mash.request_id": self.request_id}},
        )

    def mode_changed(self, at: float, mode: str = "bg") -> "RequestEvents":
        return self._add(
            RuntimeEventType.REQUEST_MODE_CHANGED.value,
            at,
            {"mode": mode, "attributes": {"mash.request.mode": mode}},
        )


def _chat_attributes(model: str) -> dict[str, Any]:
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": model,
    }


def _tool_attributes(call_id: str, name: str) -> dict[str, Any]:
    return {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.id": call_id,
        "gen_ai.tool.type": "function",
    }


def completed_request(request_id: str = "req-1", **kw: Any) -> RequestEvents:
    """accept → context → step 0 (chat, one tool) → step 1 (chat) → ok."""
    return (
        RequestEvents(request_id, **kw)
        .accepted(100.0)
        .context(100.5, 101.0)
        .chat(0, 101.0, 102.0, tokens=(100, 20), cache=(50, 10),
              tool_calls=[{"id": "c1", "name": "search", "arguments": {"q": "x"}}])
        .tool(0, "c1", "search", 102.0, 103.5, arguments={"q": "x"})
        .step_end(0, 103.6)
        .chat(1, 103.6, 104.6, tokens=(200, 30), text="answer")
        .step_end(1, 104.7)
        .end(104.8)
    )

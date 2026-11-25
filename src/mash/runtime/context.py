"""Context, history, and payload helpers for the runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from ..core.context import (
    Action,
    ActionType,
    Context,
    Message,
    MessageRole,
    Response,
    ToolCall,
)
from ..core.context import ToolResult as ContextToolResult
from ..memory.compaction import compact_conversation
from ..memory.failed_turns import format_failed_request, is_failed_turn
from ..memory.ledger import format_ledger
from ..tools.tool_results import context_content
from . import sessions

if TYPE_CHECKING:
    from .service import AgentRuntime


async def get_history_turns(
    self: "AgentRuntime",
    session_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return await sessions.history_turns(
        self.store, agent_id=self.agent_id, session_id=session_id, limit=limit
    )


async def list_sessions(self: "AgentRuntime", *, limit: int) -> list[dict[str, Any]]:
    return await sessions.list_sessions(self.store, agent_id=self.agent_id, limit=limit)


async def get_session_signals(
    self: "AgentRuntime",
    session_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return await sessions.session_signals(
        self.store, agent_id=self.agent_id, session_id=session_id, limit=limit
    )


async def get_session_total_tokens(
    self: "AgentRuntime",
    session_id: str | None = None,
) -> int:
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")
    return await sessions.session_total_tokens(
        self.store, agent_id=self.agent_id, session_id=session_id_value
    )


async def compact_session(
    self: "AgentRuntime",
    session_id: str | None = None,
    *,
    reason: str = "manual",
    session_total_tokens_reset: int = 0,
) -> tuple[Optional[str], Optional[str]]:
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")

    return await compact_conversation(
        store=self.store,
        llm=self.agent.llm,
        agent_id=self.agent_id,
        session_id=session_id_value,
        max_tokens=self.agent.config.max_tokens,
        temperature=self.agent.config.compaction_temperature,
        turn_limit=self.agent.config.compaction_turn_limit,
        reason=reason,
        session_total_tokens_reset=session_total_tokens_reset,
    )


async def build_context_with_history(
    self: "AgentRuntime",
    session_id: str,
    message: str,
    *,
    system_prompt: Any = None,
    trace_ids: list[str] | None = None,
) -> tuple[Context, list[str]]:
    """The request's context, and the trace ids of the turns it replays.

    With ``trace_ids``, those turns: the ones a request recorded at its
    context load, so a recovered request rebuilds the same context.
    """
    context = Context(
        system_prompt=system_prompt if system_prompt is not None else self.system_prompt
    )
    history: list[str] = []

    if self.agent.config.conversation_history_turns > 0 or trace_ids:
        # The latest summary checkpoint and the replayable turns after it.
        # Workflow, subagent and command turns share the session but are not
        # part of the user's conversation, so the store leaves them out.
        turns_to_include = await self.store.get_replay_turns(
            session_id=session_id,
            agent_id=self.agent_id,
            turns=self.agent.config.conversation_history_turns,
            trace_ids=trace_ids,
        )
        history = [str(turn.get("trace_id")) for turn in turns_to_include]
        # A reply's ledger goes before the user message that follows it. In
        # the reply itself the model reads it as its own writing and copies
        # it into new replies. Only the model sees it: stored turns keep
        # their text.
        # A failed request has no reply to replay. Its message and error go
        # before the next user message too, after the ledger, so "try again"
        # refers to it.
        ledger_text = ""
        failed_text = ""
        for turn in turns_to_include:
            meta = turn.get("metadata") or {}
            user_text = turn.get("user_message")
            if is_failed_turn(turn):
                failed_text = _blocks(
                    failed_text, format_failed_request(str(user_text or ""), meta)
                )
                continue
            if user_text and meta.get("type") != "summary_checkpoint":
                context.add_message(
                    MessageRole.USER,
                    _blocks(ledger_text, failed_text, user_text),
                    source="history",
                    trace_id=turn.get("trace_id"),
                )
                ledger_text = ""
                failed_text = ""

            agent_text = turn.get("agent_response")
            if agent_text:
                context.add_message(
                    MessageRole.ASSISTANT,
                    agent_text,
                    source="history",
                    trace_id=turn.get("trace_id"),
                )
            turn_ledger = format_ledger(meta.get("ledger") or [])
            if turn_ledger:
                ledger_text = turn_ledger
        message = _blocks(ledger_text, failed_text, message)

    context.add_user_message(message)
    return context, history


def _blocks(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def compute_turn_tokens(response_metadata: Dict[str, Any]) -> int:
    token_usage = response_metadata.get("token_usage")
    if not token_usage:
        return 0

    input_tokens = token_usage.get("input")
    output_tokens = token_usage.get("output")
    if input_tokens is None or output_tokens is None:
        return 0

    return int(input_tokens) + int(output_tokens)


async def build_context_payload(
    self: "AgentRuntime",
    *,
    session_id: str,
    message: str,
    system_prompt: Any = None,
) -> dict[str, Any]:
    compaction_summary_text: Optional[str] = None
    compaction_summary_trace_id: Optional[str] = None
    if self.agent.config.compaction_token_threshold > 0:
        session_total_tokens = await self.get_session_total_tokens(session_id)
        if session_total_tokens >= self.agent.config.compaction_token_threshold:
            compaction_summary_text, compaction_summary_trace_id = await self.compact_session(
                session_id,
                reason="auto",
                session_total_tokens_reset=0,
            )
    context, history = await build_context_with_history(
        self, session_id, message, system_prompt=system_prompt
    )
    return {
        "context": serialize_context(context),
        "history": history,
        "compaction": {
            "compaction_summary_text": compaction_summary_text,
            "compaction_summary_trace_id": compaction_summary_trace_id,
        },
    }


def action_from_payload(payload: dict[str, Any]) -> Action:
    action_type = str(payload.get("action_type") or "")
    if action_type == ActionType.TOOL_CALL.value:
        tool_calls = [
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                arguments=dict(item.get("arguments") or {}),
            )
            for item in payload.get("tool_calls", [])
            if isinstance(item, dict)
        ]
        return Action.from_tool_calls(tool_calls, metadata=dict(payload or {}))
    if action_type == ActionType.RESPONSE.value:
        return Action.from_response(
            str(payload.get("assistant_text") or ""),
            metadata=dict(payload or {}),
        )
    return Action.finish(metadata=dict(payload or {}))


def tool_calls_from_action_payload(
    payload: dict[str, Any],
) -> list[ToolCall]:
    return action_from_payload(payload).tool_calls


def result_payloads_to_context_results(
    payloads: list[dict[str, Any]],
    *,
    trace_id: str,
    limit: int,
) -> list[ContextToolResult]:
    """The results as the context holds them: a long one as its preview.

    The payloads keep the full content, which ``execute_tool.end`` stores.
    """
    results: list[ContextToolResult] = []
    for payload in payloads:
        result_payload = dict(payload.get("result") or {})
        result_metadata = dict(result_payload.get("metadata") or {})
        tool_name = payload.get("tool_name")
        if tool_name is not None:
            result_metadata["tool_name"] = str(tool_name)
        tool_call_id = str(payload.get("tool_call_id") or "")
        results.append(
            ContextToolResult(
                tool_call_id=tool_call_id,
                content=context_content(
                    str(result_payload.get("content") or ""),
                    limit=limit,
                    trace_id=trace_id,
                    tool_call_id=tool_call_id,
                ),
                is_error=bool(result_payload.get("is_error")),
                metadata=result_metadata,
                images=[dict(image) for image in result_payload.get("images") or []],
            )
        )
    return results


def response_from_context_payload(
    self: "AgentRuntime",
    context_payload: dict[str, Any],
) -> Response:
    context = deserialize_context(self, context_payload.get("context") or {})
    context.mark_complete()
    return Response.from_context(context)


def serialize_context(context: Context) -> dict[str, Any]:
    return {
        "system_prompt": context.system_prompt,
        "messages": [
            {
                "role": message.role.value,
                "content": message.content,
                "metadata": dict(message.metadata or {}),
            }
            for message in context.messages
        ],
        "metadata": dict(context.metadata or {}),
        "signals": dict(context.signals or {}),
        "is_complete": bool(context.is_complete),
    }


def deserialize_context(self: "AgentRuntime", payload: dict[str, Any]) -> Context:
    context = Context(
        system_prompt=payload.get("system_prompt") or self.system_prompt,
        metadata=dict(payload.get("metadata") or {}),
        signals=dict(payload.get("signals") or {}),
        is_complete=bool(payload.get("is_complete")),
    )
    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        role_text = str(item.get("role") or MessageRole.USER.value)
        try:
            role = MessageRole(role_text)
        except ValueError:
            role = MessageRole.USER
        context.messages.append(
            Message(
                role=role,
                content=item.get("content", ""),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return context

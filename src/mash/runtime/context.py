"""Context, history, and payload helpers for the runtime."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Optional

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
from ..logging import get_request_id
from ..memory.board import board_label, compact_board
from ..memory.store import BOARD_SUMMARY
from ..memory.routes import format_routes
from ..tools.tool_results import context_content
from . import sessions
from .factory import is_companion
from .transcript import render_messages

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


async def get_context_tokens(
    self: "AgentRuntime",
    session_id: str | None = None,
) -> int:
    """The size of the session's latest turn's context, in tokens."""
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")
    return await sessions.context_tokens(
        self.store, agent_id=self.agent_id, session_id=session_id_value
    )


async def compact_session(
    self: "AgentRuntime",
    session_id: str | None = None,
    *,
    reason: str = "manual",
) -> tuple[Optional[str], Optional[str]]:
    """Summarize the session; the summary's text and trace id.

    On the companion the session is a conversation, and its board gets a
    summary entry. Elsewhere every turn since the last checkpoint becomes a
    summary checkpoint, from each turn's user message and reply.
    """
    session_id_value = str(session_id or self.session_id).strip()
    if not session_id_value:
        raise ValueError("session_id is required")
    if is_companion(self):
        trace_id = str(uuid.uuid4())
        entry = await compact_board(
            self.store,
            self.agent.llm,
            session_id=session_id_value,
            agent_id=self.agent_id,
            trace_id=trace_id,
            threshold=self.agent.config.compaction_token_threshold,
            max_tokens=self.agent.config.max_tokens,
            temperature=self.agent.config.compaction_temperature,
            reason=reason,
        )
        if entry is None:
            return "", ""
        return str(entry["agent_response"]), trace_id
    turns = await self.store.get_replay_turns(session_id=session_id_value)
    messages, pending, history = _history(turns)
    if pending:
        messages.append(Message(role=MessageRole.USER, content=pending))
    replaced = [
        turn_id for turn_id, kind in history if kind != "summary"
    ]
    if not replaced:
        return "", ""
    return await compact_conversation(
        self.store,
        self.agent.llm,
        agent_id=self.agent_id,
        session_id=session_id_value,
        conversation_text=render_messages(messages),
        turn_ids=replaced,
        max_tokens=self.agent.config.max_tokens,
        temperature=self.agent.config.compaction_temperature,
        reason=reason,
    )


def _history(
    turns: list[dict[str, Any]],
) -> tuple[list[Message], str, list[tuple[str, str]]]:
    """The turns as messages, the blocks that go before the next user message,
    and each turn's trace id and kind.

    Each turn is its user message and reply. A summary checkpoint is its
    summary.
    """
    messages: list[Message] = []
    history: list[tuple[str, str]] = []
    # A reply's routes go before the user message that follows it: in the
    # reply itself the model reads them as its own writing and copies them
    # into new replies. A failed request has no reply; its message and error
    # go there too, after the routes, so "try again" refers to it. Only the
    # model sees these blocks: stored turns keep their text.
    routes_text = ""
    failed_text = ""
    for turn in turns:
        trace_id = str(turn.get("trace_id"))
        meta = turn.get("metadata") or {}
        user_text = str(turn.get("user_message") or "")
        reply = str(turn.get("agent_response") or "")
        source = {"source": "history", "trace_id": trace_id}
        if is_failed_turn(turn):
            history.append((trace_id, "failed"))
            failed_text = _blocks(failed_text, format_failed_request(user_text, meta))
            continue
        if meta.get("type") == "summary_checkpoint":
            history.append((trace_id, "summary"))
            if reply:
                messages.append(
                    Message(
                        role=MessageRole.ASSISTANT,
                        content=reply,
                        metadata={**source, "summary": True},
                    )
                )
            continue
        history.append((trace_id, "turn"))
        if user_text:
            messages.append(
                Message(
                    role=MessageRole.USER,
                    content=_blocks(routes_text, failed_text, user_text),
                    metadata=dict(source),
                )
            )
            routes_text = failed_text = ""
        if reply:
            messages.append(
                Message(role=MessageRole.ASSISTANT, content=reply, metadata=dict(source))
            )
        routes_text = format_routes(meta.get("routes") or [])
    return messages, _blocks(routes_text, failed_text), history


async def build_context_with_history(
    self: "AgentRuntime",
    session_id: str,
    message: str,
    *,
    system_prompt: Any = None,
    trace_ids: list[str] | None = None,
) -> tuple[Context, list[str]]:
    """The context of a request in the agent's own session, and the trace ids
    of the turns it replays.

    The turns are the latest summary checkpoint and the session's last
    ``AgentConfig.conversation_history_turns`` turns after it. Workflow task
    turns share the session but are not part of the conversation, so the
    store leaves them out. With ``trace_ids``, those turns: the ones a request
    recorded at its context load, so a recovered request rebuilds the same
    context. A request the companion routed reads the board instead
    (``build_board_context``).
    """
    context = Context(
        system_prompt=system_prompt if system_prompt is not None else self.system_prompt
    )
    turns: list[dict[str, Any]] = []
    history_turns = self.agent.config.conversation_history_turns
    if trace_ids is not None or history_turns > 0:
        turns = await self.store.get_replay_turns(
            session_id=session_id,
            turns=history_turns,
            trace_ids=trace_ids,
        )
    messages, pending, history = _history(turns)
    context.messages.extend(messages)
    context.add_user_message(_blocks(pending, message))
    return context, [trace_id for trace_id, _ in history]


def _blocks(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def board_messages(entries: list[dict[str, Any]]) -> tuple[list[Message], str]:
    """The board as messages, and the text that goes before the next user message.

    Each entry is a user message headed by its label, then its reply. A
    failed entry has no reply; its label and error go before the next user
    message. Labels sit only in user messages, so the model does not copy them
    into its replies.
    """
    messages: list[Message] = []
    pending = ""
    for entry in entries:
        meta = entry.get("metadata") or {}
        label = board_label(entry)
        user_text = str(entry.get("user_message") or "")
        reply = str(entry.get("agent_response") or "")
        source = {"source": "board", "seq": entry.get("seq"), "trace_id": entry.get("trace_id")}
        if is_failed_turn(entry):
            pending = _blocks(pending, label, format_failed_request(user_text, meta))
            continue
        if meta.get("type") == BOARD_SUMMARY:
            user_text = ""
        messages.append(
            Message(
                role=MessageRole.USER,
                content=_blocks(pending, f"{label}\n{user_text}".strip()),
                metadata=dict(source),
            )
        )
        pending = ""
        if reply:
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=reply,
                    metadata={**source, "summary": meta.get("type") == BOARD_SUMMARY},
                )
            )
    return messages, pending


async def build_board_context(
    self: "AgentRuntime",
    session_id: str,
    message: str,
    *,
    system_prompt: Any = None,
    up_to: int | None = None,
) -> tuple[Context, int]:
    """The context of a request the companion routed, and the board position it read.

    The board is the latest summary and every entry after it, in full, each
    labelled with its number and author; there is no turn window. The
    position is the number of the last entry read, 0 for an empty board.
    With ``up_to``, the board as it was at that position: a recovered request
    rebuilds the context its context load recorded.
    """
    context = Context(
        system_prompt=system_prompt if system_prompt is not None else self.system_prompt
    )
    entries = await self.store.read_board(session_id, up_to=up_to)
    messages, pending = board_messages(entries)
    context.messages.extend(messages)
    context.add_user_message(_blocks(pending, message))
    position = up_to if up_to is not None else max((e["seq"] for e in entries), default=0)
    return context, int(position)


# A board summary's trace id is derived from the request that wrote it, so a
# recovered request that runs the check again finds the summary stored.
BOARD_SUMMARY_NAMESPACE = uuid.UUID("0b7a52c4-3f1e-4d0a-9c86-1e5f2a7d9b43")


async def compact_board_if_full(
    self: "AgentRuntime", session_id: str, request_id: str
) -> Optional[dict[str, Any]]:
    """Write the board's summary when its last entry reached the companion's threshold.

    The companion runs this as its request starts, before it routes the
    message, with its own model and its ``compaction_token_threshold``.
    Returns the summary entry, or None.
    """
    return await compact_board(
        self.store,
        self.agent.llm,
        session_id=session_id,
        agent_id=self.agent_id,
        trace_id=str(uuid.uuid5(BOARD_SUMMARY_NAMESPACE, request_id)),
        threshold=self.agent.config.compaction_token_threshold,
        max_tokens=self.agent.config.max_tokens,
        temperature=self.agent.config.compaction_temperature,
    )


async def build_context_payload(
    self: "AgentRuntime",
    *,
    session_id: str,
    message: str,
    system_prompt: Any = None,
    conversation: bool = False,
) -> dict[str, Any]:
    """The request's context and what its context load records.

    A request the companion routed (``conversation``) reads the board and
    records the position it read; the board is compacted by the companion,
    before the request starts. Other requests replay the agent's own session
    and compact it at the agent's threshold.
    """
    if conversation:
        context, position = await build_board_context(
            self, session_id, message, system_prompt=system_prompt
        )
        return {
            "context": serialize_context(context),
            "board": {"up_to": position},
            "compaction": {},
        }
    compaction_summary_text: Optional[str] = None
    compaction_summary_trace_id: Optional[str] = None
    threshold = self.agent.config.compaction_token_threshold
    if is_companion(self):
        # The companion's session is a conversation: its board is compacted
        # once, as a whole.
        request_id = get_request_id()
        summary = await compact_board_if_full(self, session_id, request_id) if request_id else None
        if summary is not None:
            compaction_summary_text = str(summary["agent_response"])
            compaction_summary_trace_id = str(summary["trace_id"])
    elif threshold > 0 and await get_context_tokens(self, session_id) >= threshold:
        compaction_summary_text, compaction_summary_trace_id = await compact_session(
            self, session_id, reason="auto"
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
    limit: int,
) -> list[ContextToolResult]:
    """The results as the context holds them: a long saved one as a note naming its file.

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
                    metadata=result_payload.get("metadata"),
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

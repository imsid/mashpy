"""A turn's transcript, rebuilt from the event log.

A completed request's events hold everything the model saw and said in it:
the user message on ``invoke_agent.start``, each step's assistant blocks and
tool calls on ``chat.end``, and each tool's full result on ``execute_tool.end``.
Replay rebuilds a turn's messages from them, so an agent continuing a session
sees its own earlier tool calls and results, not only its replies. A result
the request saved to a file shows as the same note it showed then.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ..core.context import Message, MessageRole
from ..core.context import ToolResult as ContextToolResult
from ..tools.tool_results import context_content
from .events import RuntimeEvent, RuntimeEventType

TRANSCRIPT_EVENT_TYPES = [
    RuntimeEventType.INVOKE_AGENT_START.value,
    RuntimeEventType.CHAT_END.value,
    RuntimeEventType.EXECUTE_TOOL_END.value,
]


def _call_id(event: RuntimeEvent) -> str:
    # step_key is execute_tool.end.<loop>.<tool_call_id>; the id may hold dots.
    return str((event.step_key or "").split(".", 3)[-1])


def turn_messages(
    events: Iterable[RuntimeEvent],
    *,
    limit: int,
    reply: str = "",
) -> Optional[list[Message]]:
    """The turn's messages in order, or None when its events do not hold them all.

    ``reply`` is the turn's saved answer. It closes a transcript whose events
    end on tool results: a reply the loop made itself, such as a forwarded
    answer, has no ``chat.end``.
    """
    start: Optional[RuntimeEvent] = None
    chat_ends: dict[int, RuntimeEvent] = {}
    tool_ends: dict[tuple[int, str], RuntimeEvent] = {}
    for event in events:
        if event.event_type == RuntimeEventType.INVOKE_AGENT_START.value:
            start = start or event
        elif event.loop_index is None:
            continue
        elif event.event_type == RuntimeEventType.CHAT_END.value:
            chat_ends.setdefault(int(event.loop_index), event)
        elif event.event_type == RuntimeEventType.EXECUTE_TOOL_END.value:
            tool_ends.setdefault((int(event.loop_index), _call_id(event)), event)
    if start is None or not chat_ends:
        return None

    messages = [Message(role=MessageRole.USER, content=str((start.payload or {}).get("message") or ""))]
    for loop_index in sorted(chat_ends):
        payload = dict(chat_ends[loop_index].payload or {})
        blocks = list(payload.get("assistant_blocks") or [])
        if blocks:
            phase = payload.get("phase")
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=blocks,
                    metadata={"phase": phase} if phase else {},
                )
            )
        calls = [call for call in payload.get("tool_calls") or [] if isinstance(call, dict)]
        if not calls:
            continue
        results: list[dict[str, Any]] = []
        for call in calls:
            call_id = str(call.get("id") or "")
            end = tool_ends.get((loop_index, call_id))
            if end is None:
                return None
            result = dict((end.payload or {}).get("result") or {})
            results.append(
                ContextToolResult(
                    tool_call_id=call_id,
                    content=context_content(
                        str(result.get("content") or ""),
                        limit=limit,
                        metadata=result.get("metadata"),
                    ),
                    is_error=bool(result.get("is_error")),
                    images=[dict(image) for image in result.get("images") or []],
                ).to_dict()
            )
        messages.append(Message(role=MessageRole.TOOL, content=results))

    if messages[-1].role != MessageRole.ASSISTANT:
        if not reply:
            return None
        messages.append(Message(role=MessageRole.ASSISTANT, content=reply))
    return messages


def group_by_trace(events: Iterable[RuntimeEvent]) -> dict[str, list[RuntimeEvent]]:
    grouped: dict[str, list[RuntimeEvent]] = {}
    for event in events:
        grouped.setdefault(str(event.trace_id or ""), []).append(event)
    return grouped


def render_messages(messages: Iterable[Message]) -> str:
    """Messages as plain text, for the compaction summary."""
    lines: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, str):
            label = {"user": "User", "assistant": "Assistant"}.get(message.role.value, "Tool")
            if (message.metadata or {}).get("summary"):
                label = "Summary of the earlier conversation"
            if content:
                lines.append(f"{label}: {content}")
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                lines.append(f"Assistant: {block['text']}")
            elif kind == "tool_call":
                lines.append(f"Assistant called {block.get('name')}: {block.get('arguments')}")
            elif kind == "tool_result":
                status = "error" if block.get("is_error") else "result"
                lines.append(f"Tool {status}: {block.get('content')}")
    return "\n".join(lines)


__all__ = ["TRANSCRIPT_EVENT_TYPES", "group_by_trace", "render_messages", "turn_messages"]

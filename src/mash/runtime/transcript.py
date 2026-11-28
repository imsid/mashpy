"""Messages as plain text, for the summary of an agent's own session."""

from __future__ import annotations

from typing import Iterable

from ..core.context import Message


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


__all__ = ["render_messages"]

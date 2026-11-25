"""Large tool results: a preview in the context, the rest read on demand.

A tool result longer than the agent's ``tool_result_context_chars`` reaches the
model as its first part and a line saying how long it is and how to read on.
The full result stays in the event log, on the call's ``execute_tool.end``,
and ``ReadToolResult`` reads any part of it from there. Nothing is dropped: the
model is told what it is not seeing and how to get it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import ToolResult

# How much of a large result the context shows.
PREVIEW_CHARS = 4000
# Room for the line ReadToolResult puts above the part it returns, so its own
# result always fits the limit and is never previewed itself.
HEADER_CHARS = 300


def preview_chars(limit: int) -> int:
    """How much of a result over ``limit`` the context shows."""
    return max(1, min(PREVIEW_CHARS, int(limit) - HEADER_CHARS))


def context_content(content: str, *, limit: int, trace_id: str, tool_call_id: str) -> str:
    """What the model sees of a tool result: all of it, or a preview past ``limit``."""
    text = content if isinstance(content, str) else str(content)
    if len(text) <= limit:
        return text
    shown = preview_chars(limit)
    return (
        f"[Result is {len(text):,} chars; this is chars 0-{shown:,}. Read more with "
        f'ReadToolResult(trace_id="{trace_id}", tool_call_id="{tool_call_id}", '
        f"offset={shown}).]\n{text[:shown]}"
    )


class ReadToolResultTool:
    """Read part of an earlier tool result of this agent in this session."""

    name = "ReadToolResult"
    requires_approval = False
    parallel_safe = True
    description = (
        "Read part of an earlier tool result that was too long to show in full. "
        "A shortened result says its length and gives the trace_id and "
        "tool_call_id to pass here; read on from offset, limit chars at a time."
    )

    def __init__(
        self,
        *,
        runtime_store: Any,
        agent_id: str,
        session_id: str,
        context_chars: int,
    ) -> None:
        self._store = runtime_store
        self._agent_id = agent_id
        self._session_id = session_id
        self._max_limit = max(1, int(context_chars) - HEADER_CHARS)
        self.parameters: Dict[str, Any] = {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string"},
                "tool_call_id": {"type": "string"},
                "offset": {"type": "integer", "description": "Defaults to 0."},
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Chars to return; defaults to {self._max_limit}, "
                        f"at most {self._max_limit}."
                    ),
                },
            },
            "required": ["trace_id", "tool_call_id"],
        }

    def to_llm_format(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    @staticmethod
    def _int(value: Any, default: int) -> Optional[int]:
        if value is None:
            return default
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        trace_id = str(args.get("trace_id") or "").strip()
        tool_call_id = str(args.get("tool_call_id") or "").strip()
        if not trace_id or not tool_call_id:
            return ToolResult.error("trace_id and tool_call_id are required")
        offset = self._int(args.get("offset"), 0)
        limit = self._int(args.get("limit"), self._max_limit)
        if offset is None or offset < 0:
            return ToolResult.error("offset must be a non-negative integer")
        if limit is None or not 0 < limit <= self._max_limit:
            return ToolResult.error(f"limit must be a positive integer, at most {self._max_limit}")
        # One indexed read: this agent's tool ends in the trace, in this session.
        events = await self._store.list_events(
            self._agent_id,
            session_id=self._session_id,
            trace_id=trace_id,
            event_type_prefix="execute_tool.end",
        )
        end = next(
            (
                event
                for event in events
                if ((event.payload or {}).get("attributes") or {}).get("gen_ai.tool.call.id")
                == tool_call_id
            ),
            None,
        )
        if end is None:
            return ToolResult.error(
                f"no result of call '{tool_call_id}' in trace '{trace_id}' of this session"
            )
        content = (end.payload or {}).get("result", {}).get("content")
        text = content if isinstance(content, str) else str(content or "")
        total = len(text)
        if offset >= total and total:
            return ToolResult.error(f"offset {offset} is past the end: the result is {total:,} chars")
        stop = min(total, offset + limit)
        more = f"; read more with offset={stop}" if stop < total else "; this is the end"
        return ToolResult.success(f"[chars {offset:,}-{stop:,} of {total:,}{more}]\n{text[offset:stop]}")


__all__ = [
    "PREVIEW_CHARS",
    "ReadToolResultTool",
    "context_content",
    "preview_chars",
]

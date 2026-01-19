"""Runtime-provided tools for agent workflows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Optional

from ..context import CLIContext
from ..memory import Memory
from .tools import ToolResult, ToolSpec, format_tool_payload


class AgentRuntimeTools(ABC):
    """Abstraction for runtime-provided tools."""

    @abstractmethod
    def build_tools(self, session_id: str) -> Iterable[ToolSpec]:
        """Return tool specs that should be registered for this session."""


class MemoryTool(AgentRuntimeTools):
    """Memory-backed tools for agent workflows."""

    def __init__(self, memory: Memory, app_id: str) -> None:
        self._memory = memory
        self._app_id = app_id

    def build_tools(self, session_id: str) -> Iterable[ToolSpec]:
        app_id = self._app_id

        def _get_full_conversation(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_full_conversation",
        ) -> ToolResult:
            conversation = self._memory.get_conversation(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(conversation),
                conversation,
            )

        def _get_preferences(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_preferences",
        ) -> ToolResult:
            preferences = self._memory.get_preferences(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(preferences),
                preferences,
            )

        def _set_preferences(
            args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "set_preferences",
        ) -> ToolResult:
            if "preferences" not in args:
                return ToolResult(_name, "preferences is required.", is_error=True)
            self._memory.set_preferences(app_id, session_id, args.get("preferences"))
            return ToolResult(_name, "ok")

        return [
            ToolSpec(
                name="get_full_conversation",
                description="Return the full conversation history for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_full_conversation,
            ),
            ToolSpec(
                name="get_preferences",
                description="Fetch stored user preferences for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_preferences,
            ),
            ToolSpec(
                name="set_preferences",
                description="Store user preferences for this session.",
                input_schema={
                    "type": "object",
                    "properties": {"preferences": {}},
                    "required": ["preferences"],
                },
                source="memory",
                tags={"memory"},
                invoke=_set_preferences,
            ),
        ]

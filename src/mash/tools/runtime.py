"""Runtime tools for agent memory and preferences.

These tools are automatically available to all Mash agents and provide:
- Conversation memory access
- User preference storage
- App-specific data persistence

All tools are app-scoped for clean isolation between applications.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..memory.store import MemoryStore
from .base import FunctionTool, Tool, ToolResult


class RuntimeToolBuilder:
    """Builder for runtime tools with app and session context."""

    def __init__(
        self,
        store: MemoryStore,
        app_id: str,
        session_id: str,
    ) -> None:
        """Initialize runtime tool builder.

        Args:
            store: Conversation store for persistence.
            app_id: Application ID for isolation.
            session_id: Session ID for scoping.
        """
        self._store = store
        self._app_id = app_id
        self._session_id = session_id

    def build_tools(self) -> List[Tool]:
        """Build runtime tools for this app and session."""
        return [
            self._build_get_conversation_tool(),
            self._build_get_preferences_tool(),
            self._build_set_preferences_tool(),
            self._build_list_app_data_tool(),
            self._build_set_app_data_tool(),
        ]

    def _build_get_conversation_tool(self) -> Tool:
        """Tool to get conversation history."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            limit = args.get("limit")
            turns = self._store.get_turns(
                session_id=self._session_id,
                limit=limit,
            )
            # Format as conversation messages
            messages = []
            for turn in turns:
                messages.append(
                    {
                        "role": "user",
                        "content": turn["user_message"],
                    }
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn["agent_response"],
                    }
                )
            return ToolResult(
                content=json.dumps(messages, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_conversation",
            description=(
                "Get the conversation history for this session. "
                "Optionally limit the number of recent turns returned. "
                "Use this to reference earlier parts of the conversation "
                "instead of relying on context window."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of turns to return (optional)",
                    },
                },
            },
            _executor=execute,
        )

    def _build_get_preferences_tool(self) -> Tool:
        """Tool to get user preferences."""

        def execute(_args: Dict[str, Any]) -> ToolResult:
            preferences = self._store.get_preferences(
                app_id=self._app_id,
                session_id=self._session_id,
            )
            if preferences is None:
                return ToolResult(
                    content="No preferences stored.",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(preferences, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_preferences",
            description=(
                "Get stored user preferences for this session. "
                "Preferences are persistent across conversations. "
                "Check this at the start of conversations to maintain user context."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

    def _build_set_preferences_tool(self) -> Tool:
        """Tool to set user preferences."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            preferences = args.get("preferences")
            if not isinstance(preferences, dict):
                return ToolResult(
                    content="preferences must be a JSON object",
                    is_error=True,
                )
            self._store.set_preferences(
                app_id=self._app_id,
                session_id=self._session_id,
                preferences=preferences,
            )
            return ToolResult(
                content="Preferences saved successfully.",
                is_error=False,
            )

        return FunctionTool(
            name="set_preferences",
            description=(
                "Store user preferences for this session. "
                "Use this to remember user settings, preferences, or context "
                "that should persist across conversations (e.g., depth level, "
                "code style, focus areas, language preferences)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "preferences": {
                        "type": "object",
                        "description": "User preferences as JSON object",
                    },
                },
                "required": ["preferences"],
            },
            _executor=execute,
        )

    def _build_get_app_data_tool(self) -> Tool:
        """Tool to get app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            value = self._store.get_app_data(
                app_id=self._app_id,
                session_id=self._session_id,
                key=str(key),
            )
            if value is None:
                return ToolResult(
                    content=f"No data found for key: {key}",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(value, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_app_data",
            description=(
                "Get app-specific data by key. "
                "Use this to retrieve information previously stored "
                "with set_app_data (e.g., file locations, discovered patterns, "
                "configurations)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key to retrieve",
                    },
                },
                "required": ["key"],
            },
            _executor=execute,
        )

    def _build_set_app_data_tool(self) -> Tool:
        """Tool to set app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            value = args.get("value")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            if value is None:
                return ToolResult(
                    content="value is required",
                    is_error=True,
                )
            self._store.set_app_data(
                app_id=self._app_id,
                session_id=self._session_id,
                key=str(key),
                value=value,
            )
            return ToolResult(
                content=f"Data stored successfully for key: {key}",
                is_error=False,
            )

        return FunctionTool(
            name="set_app_data",
            description=(
                "Store app-specific data by key. "
                "Use this to persist information that should be available "
                "in future conversations (e.g., discovered file locations, "
                "identified patterns, user-specific configs, important facts). "
                "This helps build up knowledge over time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key (use as a tag/identifier)",
                    },
                    "value": {
                        "description": "Data value (any JSON-serializable type)",
                    },
                },
                "required": ["key", "value"],
            },
            _executor=execute,
        )

    def _build_list_app_data_tool(self) -> Tool:
        """Tool to list all app-specific data."""

        def execute(_args: Dict[str, Any]) -> ToolResult:
            data = self._store.list_app_data(
                app_id=self._app_id,
                session_id=self._session_id,
            )
            if not data:
                return ToolResult(
                    content="No app data stored.",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(data, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="list_app_data",
            description=(
                "List all app-specific data stored for this session. "
                "Returns all keys and values previously saved with set_app_data. "
                "Use this to see what information you've accumulated."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

    def _build_delete_app_data_tool(self) -> Tool:
        """Tool to delete app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            deleted = self._store.delete_app_data(
                app_id=self._app_id,
                session_id=self._session_id,
                key=str(key),
            )
            if deleted:
                return ToolResult(
                    content=f"Data deleted successfully for key: {key}",
                    is_error=False,
                )
            else:
                return ToolResult(
                    content=f"No data found for key: {key}",
                    is_error=False,
                )

        return FunctionTool(
            name="delete_app_data",
            description=(
                "Delete app-specific data by key. "
                "Use this to remove information that is no longer needed "
                "or has become outdated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key to delete",
                    },
                },
                "required": ["key"],
            },
            _executor=execute,
        )

__all__ = [
    "RuntimeToolBuilder",
]
